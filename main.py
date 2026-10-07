import asyncio
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi, ReplyMessageRequest, TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()
logger = logging.getLogger(__name__)


def required_env(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


DEEPL_API_KEY = required_env('DEEPL_API_KEY')
LINE_CHANNEL_SECRET = required_env('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = required_env('LINE_CHANNEL_ACCESS_TOKEN')
# API Free keys end in :fx; allow an explicit override for proxies/custom setups.
DEEPL_URL = os.getenv('DEEPL_URL') or (
    'https://api-free.deepl.com/v2/translate' if DEEPL_API_KEY.endswith(':fx')
    else 'https://api.deepl.com/v2/translate'
)
app = FastAPI()
parser = WebhookParser(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


class TranslationError(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f'DeepL failed (status={status or "network/response"})')


def deepl_translate(text, target_lang, source_lang=None):
    payload = {'text': [text], 'target_lang': target_lang}
    if source_lang:
        payload['source_lang'] = source_lang
    # Bounded retries: two translations must still fit LINE's reply-token lifetime.
    for attempt in range(2):
        try:
            response = requests.post(
                DEEPL_URL,
                headers={'Authorization': f'DeepL-Auth-Key {DEEPL_API_KEY}'},
                json=payload, timeout=(3, 7),
            )
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 0:
                time.sleep(0.5)
                continue
            raise TranslationError() from None
        except requests.RequestException:
            raise TranslationError() from None
        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt == 0:
                time.sleep(0.5)
                continue
        if response.status_code != 200:
            raise TranslationError(response.status_code)
        try:
            result = response.json()['translations'][0]
            translated = result['text']
            source = result.get('detected_source_language') or source_lang
            if not isinstance(translated, str) or not translated.strip() or not source:
                raise ValueError('Invalid translation response')
            return {'text': translated, 'source_language': source.upper()}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise TranslationError() from None
    raise TranslationError()


def translate_message(text):
    # Explicit direction is useful for short/ambiguous or mixed-language messages.
    command = re.match(r'^/(ja|en)\s+(.+)$', text, flags=re.I | re.S)
    if command:
        target, content = command.group(1).upper(), command.group(2).strip()
        return ('🇯🇵 ' if target == 'JA' else '🇺🇸 ') + deepl_translate(content, target)['text']
    # Kana is evidence of Japanese; kanji alone is also used in Chinese.
    if re.search(r'[ぁ-ゖァ-ヺｦ-ﾟ]', text):
        return '🇺🇸 ' + deepl_translate(text, 'EN', 'JA')['text']
    ja_result = deepl_translate(text, 'JA')
    source = ja_result['source_language']
    logger.info('Translation direction source=%s', source)
    if source == 'JA':
        return '🇺🇸 ' + deepl_translate(text, 'EN', 'JA')['text']
    if source.startswith('EN'):
        return '🇯🇵 ' + ja_result['text']
    return '🇯🇵 ' + ja_result['text'] + '\n\n🇺🇸 ' + deepl_translate(text, 'EN', source)['text']


def split_reply(text):
    # LINE counts UTF-16 code units; keep emoji/supplementary characters intact.
    parts, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 5000:
            parts.append(''.join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append(''.join(current))
    if len(parts) > 5:
        return ['翻訳結果が長すぎるので、メッセージを分けて送ってね。\nPlease split your message into smaller parts.']
    return parts


# Per-process duplicate protection, bounded in size and time.
_completed = OrderedDict()
_inflight = set()
_event_lock = threading.Lock()


def handle_message(event):
    if getattr(event, 'mode', 'active') == 'standby' or not event.reply_token:
        return
    text = event.message.text.strip()
    if not text or not any(char.isalpha() for char in text):
        return
    event_id = event.webhook_event_id or event.message.id
    with _event_lock:
        now = time.monotonic()
        while _completed and (now - next(iter(_completed.values())) > 1200 or len(_completed) > 10000):
            _completed.popitem(last=False)
        if event_id in _completed:
            return
        if event_id in _inflight:
            raise RuntimeError('Event already processing; retry later')
        _inflight.add(event_id)
    try:
        try:
            translated = translate_message(text)
        except TranslationError as exc:
            logger.warning('Translation failed event=%s status=%s', event_id, exc.status)
            if exc.status == 456:
                translated = '翻訳サービスの利用上限に達しているよ。管理者に連絡してね。\nTranslation quota exceeded. Please contact the administrator.'
            elif exc.status in (401, 403):
                translated = '翻訳サービスの設定エラーが起きているよ。管理者に連絡してね。\nTranslation service configuration error. Please contact the administrator.'
            else:
                translated = '今は翻訳できなかったよ。少し待ってもう一度送ってね。\nTranslation failed. Please try again shortly.'
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=part) for part in split_reply(translated)],
                ), _request_timeout=(3, 7),
            )
        with _event_lock:
            _completed[event_id] = time.monotonic()
    finally:
        with _event_lock:
            _inflight.discard(event_id)


@app.get('/')
def root():
    return {'status': 'LINE Translation Bot is running'}


@app.post('/callback')
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        raise HTTPException(status_code=400, detail='Missing signature')
    try:
        body = (await request.body()).decode('utf-8')
        events = parser.parse(body, signature)
    except (InvalidSignatureError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail='Invalid webhook') from None
    results = await asyncio.gather(*(
        run_in_threadpool(handle_message, event) for event in events
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent)
    ), return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        # Do not log SDK exception bodies: they may contain conversation data/tokens.
        for result in results:
            if isinstance(result, BaseException):
                logger.error('Webhook processing failed type=%s status=%s', type(result).__name__, getattr(result, 'status', None))
        raise HTTPException(status_code=500, detail='Message processing failed')
    return 'OK'

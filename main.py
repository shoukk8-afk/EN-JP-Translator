import os
import requests

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent


# =========================
# 環境変数
# =========================

load_dotenv()

DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")


# =========================
# FastAPI / LINE
# =========================

app = FastAPI()

handler = WebhookHandler(LINE_CHANNEL_SECRET)

configuration = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)


# =========================
# DeepL
# =========================

DEEPL_URL = "https://api-free.deepl.com/v2/translate"


def deepl_translate(text: str, target_lang: str):
    """
    DeepLで翻訳する。
    source_langは指定せず、DeepLに自動判定させる。
    """

    response = requests.post(
        DEEPL_URL,
        headers={
            "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"
        },
        json={
            "text": [text],
            "target_lang": target_lang
        },
        timeout=10
    )

    response.raise_for_status()

    result = response.json()["translations"][0]

    return {
        "text": result["text"],
        "source_language": result["detected_source_language"]
    }


def translate_message(text: str) -> str:
    """
    JA → EN
    EN → JA
    その他 → JA + EN
    """

    # まず日本語へ翻訳して、入力言語を判定
    ja_result = deepl_translate(text, "JA")

    source_language = ja_result["source_language"]

    print("Detected language:", source_language)

    # -------------------------
    # 日本語 → 英語
    # -------------------------

    if source_language == "JA":

        en_result = deepl_translate(text, "EN")

        return f"🇺🇸 {en_result['text']}"

    # -------------------------
    # 英語 → 日本語
    # -------------------------

    elif source_language == "EN":

        return f"🇯🇵 {ja_result['text']}"

    # -------------------------
    # その他 → 日本語 + 英語
    # -------------------------

    else:

        en_result = deepl_translate(text, "EN")

        return (
            f"🇯🇵 {ja_result['text']}\n\n"
            f"🇺🇸 {en_result['text']}"
        )


# =========================
# FastAPI
# =========================

@app.get("/")
def root():
    return {
        "status": "LINE Translation Bot is running"
    }


@app.post("/callback")
async def callback(request: Request):

    signature = request.headers.get("X-Line-Signature")

    if not signature:
        raise HTTPException(status_code=400)

    body = (await request.body()).decode("utf-8")

    try:
        handler.handle(body, signature)

    except InvalidSignatureError:
        raise HTTPException(status_code=400)

    return "OK"


# =========================
# LINE メッセージ処理
# =========================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    original_text = event.message.text

    try:

        translated_text = translate_message(original_text)

        with ApiClient(configuration) as api_client:

            line_bot_api = MessagingApi(api_client)

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(
                            text=translated_text
                        )
                    ]
                )
            )

    except Exception as e:

        print("ERROR:", e)

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


load_dotenv()

DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")


app = FastAPI()

handler = WebhookHandler(LINE_CHANNEL_SECRET)

configuration = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)


def translate(text: str) -> str:
    # 日本語（ひらがな・カタカナ・漢字）が含まれているか
    contains_japanese = any(
        '\u3040' <= char <= '\u30ff' or
        '\u4e00' <= char <= '\u9fff'
        for char in text
    )

    # 日本語なら英語へ、それ以外なら日本語へ
    target_lang = "EN" if contains_japanese else "JA"

    response = requests.post(
        "https://api-free.deepl.com/v2/translate",
        headers={
            "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"
        },
        json={
            "text": [text],
            "target_lang": target_lang
        }
    )

    response.raise_for_status()

    return response.json()["translations"][0]["text"]


@app.get("/")
def root():
    return {"status": "LINE Translation Bot is running"}


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


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    original_text = event.message.text

    try:
        translated_text = translate(original_text)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=translated_text)
                    ]
                )
            )

    except Exception as e:
        print("ERROR:", e)

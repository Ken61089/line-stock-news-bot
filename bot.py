"""
LINE 機器人(Webhook 模式)。

用法:在 LINE 裡把財經新聞「標題 + 內文(可含連結)」整段傳給你的官方帳號,
機器人會自動呼叫 Claude 解析、寫進 Google Sheet,再回覆你重點摘要。

小技巧:傳一個字「id」給機器人,它會回你的 LINE user id
(設定 LINE_ALLOWED_USER_ID 白名單時會用到)。
"""

import os
import threading
import logging

from flask import Flask, request, abort
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from news_processor import route_and_store, NoCategoryError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("line-news-bot")

# 必填(沒設定會在啟動時直接報錯提醒)
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
# 只允許你本人使用(填你的 LINE user id;傳「id」給機器人可查)。留空 = 不限制。
ALLOWED_USER_ID = os.environ.get("LINE_ALLOWED_USER_ID", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__)
parser = WebhookParser(CHANNEL_SECRET)
_config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


# ---- LINE 傳訊小工具 ----
def _reply(reply_token: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


def _push(user_id: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )


def _say(reply_token: str, user_id: str, text: str) -> None:
    """優先用 reply(免費、不限量);萬一 token 過期,改用 push 當備援。"""
    try:
        _reply(reply_token, text)
    except Exception:  # noqa: BLE001
        logger.warning("reply 失敗,改用 push")
        if user_id:
            _push(user_id, text)


# ---- 背景處理一則新聞,完成後回覆 ----
def _process_and_reply(reply_token: str, user_id: str, text: str) -> None:
    try:
        result = route_and_store(text)
    except NoCategoryError as e:
        # 第一行沒標分類、或沒內容 → 回提示(不是錯誤)
        _say(reply_token, user_id, str(e))
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("處理失敗")
        _say(reply_token, user_id, f"❌ 處理失敗:{e}")
        return

    _say(reply_token, user_id, result.reply)


# ---- 健康檢查(打開網址會看到 OK,確認服務有在跑)----
@app.get("/")
def health():
    return "OK, news bot is running.", 200


# ---- LINE Webhook 入口 ----
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        abort(400)

    for event in events:
        if not isinstance(event, MessageEvent):
            continue
        if not isinstance(event.message, TextMessageContent):
            continue

        user_id = getattr(event.source, "user_id", "") or ""
        text = (event.message.text or "").strip()
        reply_token = event.reply_token

        # 查自己的 user id
        if text.lower() == "id":
            _say(reply_token, user_id, f"你的 LINE user id:\n{user_id}")
            continue

        # 白名單檢查
        if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
            _say(reply_token, user_id, "⛔ 抱歉,這是私人機器人。")
            continue

        # 解析+寫入會等網路,放背景執行緒,Webhook 先快速回 200 給 LINE
        threading.Thread(
            target=_process_and_reply,
            args=(reply_token, user_id, text),
            daemon=True,
        ).start()

    return "OK", 200


if __name__ == "__main__":
    from waitress import serve

    logger.info("LINE 機器人啟動,監聽埠口 %s ...", PORT)
    serve(app, host="0.0.0.0", port=PORT)

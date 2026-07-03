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

from news_processor import route_and_store, NoCategoryError, is_group_additive_command
import calendar_notify

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

# 掛上「Notion 行事曆 → LINE 群組」定時推播排程(每日 07:00 當日、每週日 21:00 下週)
calendar_notify.maybe_start_scheduler()


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
APP_VERSION = "2026-07-03-group-commands"  # 每次改版更新,方便用網址確認線上是否為新版


@app.get("/")
def health():
    return f"OK, news bot is running. [{APP_VERSION}]", 200


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
        group_id = getattr(event.source, "group_id", "") or ""
        room_id = getattr(event.source, "room_id", "") or ""
        text = (event.message.text or "").strip()
        reply_token = event.reply_token

        # 查 id:在群組/聊天室會一併回傳 group/room id(設定推播目標時要用)
        if text.lower() == "id":
            lines = [f"你的 LINE user id:\n{user_id}"]
            if group_id:
                lines.append(f"\n本群組 group id(推播目標請用這個):\n{group_id}")
            if room_id:
                lines.append(f"\n本聊天室 room id(推播目標請用這個):\n{room_id}")
            _say(reply_token, user_id, "\n".join(lines))
            continue

        # 群組/聊天室:團隊成員可下「新增/查詢類」指令(時程/關注/更新法說會/存新聞/查詢),
        # 一般聊天與破壞性指令(改股/主表修正)一律忽略,避免洗版與誤改。
        if group_id or room_id:
            if is_group_additive_command(text):
                threading.Thread(
                    target=_process_and_reply,
                    args=(reply_token, user_id, text),
                    daemon=True,
                ).start()
            continue

        # 白名單檢查(1 對 1):非本人 → 靜默忽略,不回訊息
        if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
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

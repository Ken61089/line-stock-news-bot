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
import fetchers
import eps_tracker
import price_alerts

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


# ---- 背景執行「股訊」查詢(個股已存新聞/事件)並回覆 ----
def _reply_stock_info(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = calendar_notify.run_stock_info(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("股訊查詢失敗")
        reply = f"❌ 股訊查詢失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 背景執行「營收」查詢(公布日曆預估)並回覆 ----
def _reply_revenue(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = fetchers.run_revenue_query(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("營收查詢失敗")
        reply = f"❌ 營收查詢失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 背景執行「融資」查詢(兩市融資餘額增減)並回覆 ----
def _reply_margin(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = fetchers.run_margin_query(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("融資查詢失敗")
        reply = f"❌ 融資查詢失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 背景執行「EPS」查詢(財報每股盈餘表)並回覆 ----
def _reply_eps(reply_token: str, user_id: str, text: str) -> None:
    # 「EPS 回補」要跑好幾分鐘(16 次彙總請求 + 數百次 Notion 寫入),遠超過 reply token 時效,
    # 所以先立刻回一則「開始回補」,跑完不推播(省 LINE 額度),使用者稍後自己查表。
    if eps_tracker.is_backfill_command(text):
        codes = eps_tracker.backfill_target(text)
        if codes == []:  # 有指定但查無此檔(打錯代號)→ 不能當成「補全部」
            _say(reply_token, user_id, "找不到這一檔,請確認代號或名稱在個股主表裡。\n要補全部就只打「EPS 回補」。")
            return
        who = f"{codes[0]} 這一檔" if codes else "全部追蹤股"
        mins = "約 3 分鐘" if codes else "約 5~8 分鐘"
        _say(reply_token, user_id,
             f"⏳ 開始回補{who}近 8 季 EPS,{mins}。\n完成後打「EPS 代號」看表格(不另外推播)。")
        try:
            res = eps_tracker.manual_backfill(codes=codes)
            logger.info("EPS 回補完成(%s):%s", who, res)
        except Exception:  # noqa: BLE001
            logger.exception("EPS 回補失敗")
        return
    try:
        reply = eps_tracker.run_eps_query(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("EPS 查詢失敗")
        reply = f"❌ EPS 查詢失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 背景執行「提醒」設定/查詢並回覆 ----
def _reply_alert(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = price_alerts.run_alert_command(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("提醒指令失敗")
        reply = f"❌ 提醒指令失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 背景執行「列出事件」查詢並回覆 ----
def _reply_list_events(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = calendar_notify.run_list_events(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("列出事件失敗")
        reply = f"❌ 列出事件失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 背景執行「早報」(今日行事曆+法說會預告)並回覆 ----
def _reply_today_brief(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = calendar_notify.run_today_brief()
    except Exception as e:  # noqa: BLE001
        logger.exception("早報失敗")
        reply = f"❌ 早報失敗:{e}"
    _say(reply_token, user_id, reply)


# ---- 背景執行「用量」報告並回覆 ----
def _reply_usage(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = calendar_notify.run_usage()
    except Exception as e:  # noqa: BLE001
        logger.exception("用量報告失敗")
        reply = f"❌ 用量報告失敗:{e}"
    _say(reply_token, user_id, reply)


# ---- 背景執行「查重訊」查詢並回覆 ----
def _reply_alerts_query(reply_token: str, user_id: str, text: str) -> None:
    try:
        reply = calendar_notify.run_alerts_query(text)
    except Exception as e:  # noqa: BLE001
        logger.exception("查重訊失敗")
        reply = f"❌ 查重訊失敗:{e}"
    if reply:
        _say(reply_token, user_id, reply)


# ---- 健康檢查(打開網址會看到 OK,確認服務有在跑)----
APP_VERSION = "2026-08-11-antihallucination"  # 每次改版更新,方便用網址確認線上是否為新版


@app.get("/")
def health():
    return f"OK, news bot is running. [{APP_VERSION}]", 200


@app.get("/schedule")
def schedule_info():
    """回報線上**實際生效**的排程與開關,不含任何金鑰。
    用途:確認 Zeabur 後台的環境變數到底設了什麼,不必進後台翻。

    ⚠️ jobs 讀的是 APScheduler 裡真正掛上的 job,不是從 env 預設值反推 ——
    本機 .env 與 Zeabur 後台常常不同步(本機根本沒有 NOTIFY_DAILY_TIME,
    線上卻設了值),猜預設值會給出錯的答案。"""
    e = os.environ.get
    return {
        "version": APP_VERSION,
        "jobs": calendar_notify.scheduler_jobs(),
        "主動推播總開關": "開" if calendar_notify.is_push_enabled() else "關",
        "重訊命中即時推播": "開" if e("MOPS_ALERT_PUSH", "0").strip() != "0" else "關(只記 Notion)",
        "明日彙整": "開" if e("ENABLE_TOMORROW_NOTIFY", "0").strip() != "0" else "關",
        "本人白名單": "已設" if e("LINE_ALLOWED_USER_ID", "").strip() else "未設(破壞性指令與 EPS 回補的限制會失效)",
        "推播目標": "已設" if e("LINE_NOTIFY_TARGET_ID", "").strip() else "未設",
    }, 200


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

        # 「推播開啟/關閉/狀態」總開關:僅本人私訊可改(config 變更)
        toggle = calendar_notify.is_push_toggle_command(text)
        if toggle:
            if not ALLOWED_USER_ID or user_id == ALLOWED_USER_ID:
                _say(reply_token, user_id, calendar_notify.run_push_toggle(toggle))
            continue

        # 「早報/今日」→ 即時叫出今日行事曆+法說會預告(reply,不計額度),唯讀群組私訊都可
        if calendar_notify.is_today_brief_command(text):
            threading.Thread(
                target=_reply_today_brief,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「用量」報告:LINE 推播則數 + Firecrawl credit + AI token 即時用量,唯讀
        if calendar_notify.is_usage_command(text):
            threading.Thread(
                target=_reply_usage,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「股訊 2303」查個股已存新聞/事件(「股訊 2303 2」展開第2則):唯讀,群組與私訊都可用
        if calendar_notify.is_stock_info_command(text):
            threading.Thread(
                target=_reply_stock_info,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「營收日曆」/「營收 2330」查月營收公布日預估:唯讀,群組與私訊都可用
        if fetchers.is_revenue_command(text):
            threading.Thread(
                target=_reply_revenue,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「融資」查兩市融資餘額增減(可帶日期,如「融資 7/28」):唯讀,群組與私訊都可用
        if fetchers.is_margin_command(text):
            threading.Thread(
                target=_reply_margin,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「EPS 2330」查該檔各季單季/累計每股盈餘:唯讀,群組與私訊都可用。
        # 但「EPS 回補」會打幾百次 Notion API + 抓 16 次 1.6MB 彙總表、跑 5 分鐘以上,
        # 被群組成員誤觸代價不小 → 僅限本人。
        if eps_tracker.is_eps_command(text):
            if eps_tracker.is_backfill_command(text) and ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
                _say(reply_token, user_id, "「EPS 回補」僅限本人使用。查詢請打「EPS 代號」。")
                continue
            threading.Thread(
                target=_reply_eps,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「提醒 大盤 跌 1000 …」設盤中事件提醒;「提醒清單」/「刪提醒 N」管理。
        # 觸發時推的是本人私訊,所以設定也限本人(免得群組成員設了卻推到我這裡)。
        if price_alerts.is_alert_command(text):
            if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
                _say(reply_token, user_id, "「提醒」僅限本人設定。")
                continue
            threading.Thread(
                target=_reply_alert,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「查重訊」查詢追蹤股已累積的重大訊息(某天/本週/區間):唯讀,群組與私訊都可用
        if calendar_notify.is_alerts_query_command(text):
            threading.Thread(
                target=_reply_alerts_query,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 「列出事件」查詢(本週/下週/指定日期):唯讀,群組與私訊都可用
        if calendar_notify.is_list_events_command(text):
            threading.Thread(
                target=_reply_list_events,
                args=(reply_token, user_id, text),
                daemon=True,
            ).start()
            continue

        # 群組/聊天室:團隊成員可下「新增類」指令(時程/關注/更新法說會/存新聞),
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

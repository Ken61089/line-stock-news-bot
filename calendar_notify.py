"""
Notion 行事曆 → LINE 群組定時推播。

把 Notion「3. 新聞與時程動態庫」裡有「關鍵日期」的事件(法說會/股東會/CB掛牌/
CB拆解/經濟數據公布…等),依排程主動推播到指定 LINE 群組。

排程(台灣時間,可用環境變數覆寫):
  - 每天 07:00      → 推「今天」的事件(當天沒事件則不發,避免洗版)
  - 每週日 21:00    → 推「下週(下週一~週日)」的行事曆彙整

需要的環境變數:
  LINE_CHANNEL_ACCESS_TOKEN   LINE Messaging API 金鑰(既有)
  LINE_NOTIFY_TARGET_ID       推播目標:群組 groupId(或個人 userId)。把機器人拉進
                              群組後,在群裡傳「id」給機器人即可查到。
  NOTION_TOKEN / NOTION_TIMELINE_DS   時程庫(沿用 notion_timeline.py 的設定)
  NOTIFY_DAILY_TIME           每日推播時刻,預設 "07:00"
  NOTIFY_WEEKLY_TIME          每週推播時刻,預設 "21:00"
  NOTIFY_WEEKLY_DOW           每週推播星期(0=一…6=日),預設 "6"(週日)
  NOTIFY_EXCLUDE_TYPES        不推播的事件類型(逗號分隔),預設空
  ENABLE_SCHEDULER            設 "0" 可停用內建排程,預設啟用
"""

import os
import logging
import datetime

import httpx

import notion_timeline as nt

logger = logging.getLogger("line-news-bot.calendar")

TW_TZ = nt.TW_TZ  # UTC+8,固定偏移(台灣無日光節約)

# 各事件類型前面的小圖示(沒對到的用預設)
TYPE_EMOJI = {
    "法說會": "🎤", "股東會": "🏛️", "CB掛牌": "📈", "CB拆解": "✂️",
    "發行可轉債": "💵", "除權息": "💰", "增減資": "🔀", "營收公布": "📊",
    "財報公布": "📋", "財報利空/多": "⚠️", "擴廠進度": "🏭", "試產進度": "🧪",
    "量產進度": "🚀", "展覽/政策": "📰", "盤後隨筆": "📝", "其他": "📌",
}
DEFAULT_EMOJI = "📌"

_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _target_id() -> str:
    return os.environ.get("LINE_NOTIFY_TARGET_ID", "").strip()


def _exclude_types() -> set:
    raw = os.environ.get("NOTIFY_EXCLUDE_TYPES", "").strip()
    return {t.strip() for t in raw.split(",") if t.strip()}


# ---- 查 Notion 事件 ----
def query_events(start: datetime.date, end: datetime.date) -> list[dict]:
    """查「關鍵日期」落在 [start, end](含兩端)的事件,依日期排序。
    回傳 [{'start','end','type','title','url','status'}]。"""
    payload = {
        "filter": {
            "and": [
                {"property": "關鍵日期", "date": {"on_or_after": start.isoformat()}},
                {"property": "關鍵日期", "date": {"on_or_before": end.isoformat()}},
            ]
        },
        "sorts": [{"property": "關鍵日期", "direction": "ascending"}],
        "page_size": 100,
    }
    data = nt._post(f"/data_sources/{nt.TIMELINE_DS}/query", payload)
    exclude = _exclude_types()
    events: list[dict] = []
    for pg in data.get("results", []):
        p = pg.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in p.get("Name", {}).get("title", [])).strip()
        etype = (p.get("事件類型", {}).get("select") or {}).get("name", "") or "其他"
        if etype in exclude:
            continue
        date = p.get("關鍵日期", {}).get("date") or {}
        status = (p.get("狀態", {}).get("select") or {}).get("name", "")
        events.append({
            "start": (date.get("start") or "")[:10],
            "end": (date.get("end") or "")[:10] if date.get("end") else "",
            "type": etype,
            "title": title or "(未命名事件)",
            "url": p.get("來源連結", {}).get("url") or "",
            "status": status,
        })
    return events


# ---- 格式化訊息 ----
def _fmt_line(ev: dict, with_date: bool = False) -> str:
    emoji = TYPE_EMOJI.get(ev["type"], DEFAULT_EMOJI)
    prefix = ""
    if with_date:
        d = ev["start"]
        try:
            dd = datetime.date.fromisoformat(d)
            prefix = f"{dd.month}/{dd.day}(週{_WEEKDAY_ZH[dd.weekday()]}) "
        except ValueError:
            prefix = f"{d} "
    span = ""
    if ev["end"] and ev["end"] != ev["start"]:
        try:
            e = datetime.date.fromisoformat(ev["end"])
            span = f"(至 {e.month}/{e.day})"
        except ValueError:
            span = f"(至 {ev['end']})"
    return f"{emoji} {prefix}[{ev['type']}] {ev['title']}{span}".rstrip()


def format_daily(events: list[dict], day: datetime.date) -> str:
    head = f"☀️ 今日投資行事曆 {day.month}/{day.day}(週{_WEEKDAY_ZH[day.weekday()]})"
    body = "\n".join(_fmt_line(ev) for ev in events)
    return f"{head}\n\n{body}"


def format_weekly(events: list[dict], start: datetime.date, end: datetime.date) -> str:
    head = (
        f"🗓️ 下週投資行事曆\n"
        f"{start.month}/{start.day}(週{_WEEKDAY_ZH[start.weekday()]}) ~ "
        f"{end.month}/{end.day}(週{_WEEKDAY_ZH[end.weekday()]})"
    )
    if not events:
        return f"{head}\n\n(下週目前沒有排定事件)"
    body = "\n".join(_fmt_line(ev, with_date=True) for ev in events)
    return f"{head}\n\n{body}"


# ---- 推播到 LINE ----
def push(text: str, target_id: str = "") -> None:
    target = target_id or _target_id()
    if not target:
        logger.warning("未設定 LINE_NOTIFY_TARGET_ID,略過推播。訊息內容:\n%s", text)
        return
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    # 直接呼叫 LINE push API(不依賴 bot.py,方便獨立測試)
    r = httpx.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": target, "messages": [{"type": "text", "text": text[:4900]}]},
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"LINE push {r.status_code}: {r.text[:300]}")
    logger.info("已推播到 %s(%d 字)", target[:8] + "…", len(text))


# ---- 兩個排程任務 ----
def notify_daily(dry_run: bool = False) -> str | None:
    today = datetime.datetime.now(TW_TZ).date()
    events = query_events(today, today)
    if not events:
        logger.info("今日(%s)無事件,不推播。", today)
        return None
    msg = format_daily(events, today)
    if dry_run:
        return msg
    push(msg)
    return msg


def notify_weekly(dry_run: bool = False) -> str | None:
    today = datetime.datetime.now(TW_TZ).date()
    # 下週一 = 今天之後最近的週一;下週日 = 下週一 + 6
    next_mon = today + datetime.timedelta(days=(7 - today.weekday()))
    next_sun = next_mon + datetime.timedelta(days=6)
    events = query_events(next_mon, next_sun)
    msg = format_weekly(events, next_mon, next_sun)
    if dry_run:
        return msg
    push(msg)
    return msg


# ---- 內建排程(在 web 服務啟動時掛上)----
_scheduler = None


def maybe_start_scheduler() -> None:
    global _scheduler
    if os.environ.get("ENABLE_SCHEDULER", "1").strip() == "0":
        logger.info("ENABLE_SCHEDULER=0,不啟動排程。")
        return
    if _scheduler is not None:
        return
    if not nt.enabled():
        logger.warning("未設定 NOTION_TOKEN,排程不啟動。")
        return
    if not _target_id():
        logger.warning("未設定 LINE_NOTIFY_TARGET_ID,排程不啟動(先把機器人拉進群組傳『id』取得)。")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        import pytz
    except ImportError:
        logger.exception("缺少 apscheduler/pytz,排程無法啟動(requirements.txt 需含 apscheduler、pytz)。")
        return

    tz = pytz.timezone("Asia/Taipei")
    d_h, d_m = _parse_hhmm(os.environ.get("NOTIFY_DAILY_TIME", "07:00"), 7, 0)
    w_h, w_m = _parse_hhmm(os.environ.get("NOTIFY_WEEKLY_TIME", "21:00"), 21, 0)
    w_dow = int(os.environ.get("NOTIFY_WEEKLY_DOW", "6"))  # 0=一…6=日

    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(
        _safe(notify_daily), CronTrigger(hour=d_h, minute=d_m, timezone=tz),
        id="daily", replace_existing=True,
    )
    sched.add_job(
        _safe(notify_weekly), CronTrigger(day_of_week=w_dow, hour=w_h, minute=w_m, timezone=tz),
        id="weekly", replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    logger.info(
        "行事曆排程已啟動:每日 %02d:%02d 推當日、每週 %s %02d:%02d 推下週(台灣時間)。",
        d_h, d_m, _WEEKDAY_ZH[w_dow], w_h, w_m,
    )


def _parse_hhmm(s: str, dh: int, dm: int) -> tuple[int, int]:
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return dh, dm


def _safe(fn):
    def wrapper():
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("排程任務執行失敗:%s", getattr(fn, "__name__", fn))
    return wrapper


# ---- CLI:本機測試 ----
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv

    if mode == "daily":
        out = notify_daily(dry_run=dry)
    elif mode == "weekly":
        out = notify_weekly(dry_run=dry)
    else:
        print("用法:python calendar_notify.py [daily|weekly] [--dry]")
        sys.exit(1)

    if dry:
        print("=== DRY RUN(不推播,僅顯示訊息)===")
        print(out or "(無事件,不會推播)")
    else:
        print("已執行" + mode + (",無事件未推播" if out is None else ",已推播"))

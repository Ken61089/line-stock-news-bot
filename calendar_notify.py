"""
Notion 行事曆 → LINE 群組定時推播。

把 Notion「3. 新聞與時程動態庫」裡有「關鍵日期」的事件(法說會/股東會/CB掛牌/
CB拆解/經濟數據公布…等),依排程主動推播到指定 LINE 群組。

排程(台灣時間,可用環境變數覆寫):
  - 每天 07:00      → 推「今天」的事件(當天沒事件則不發,避免洗版)
  - 每天 21:00      → 推「明天」的事件(明天沒事件則不發)
  - 每週日 21:00    → 推「下週(下週一~週日)」的行事曆彙整

需要的環境變數:
  LINE_CHANNEL_ACCESS_TOKEN   LINE Messaging API 金鑰(既有)
  LINE_NOTIFY_TARGET_ID       推播目標:群組 groupId(或個人 userId)。把機器人拉進
                              群組後,在群裡傳「id」給機器人即可查到。
  NOTION_TOKEN / NOTION_TIMELINE_DS   時程庫(沿用 notion_timeline.py 的設定)
  NOTIFY_DAILY_TIME           每日推「當日」時刻,預設 "07:00"
  NOTIFY_TOMORROW_TIME        每日推「明日」時刻,預設 "21:00"
  NOTIFY_WEEKLY_TIME          每週推「下週」時刻,預設 "21:00"
  NOTIFY_WEEKLY_DOW           每週推播星期(0=一…6=日),預設 "6"(週日)
  NOTIFY_EXCLUDE_TYPES        不推播的事件類型(逗號分隔),預設空
  ENABLE_SCHEDULER            設 "0" 可停用內建排程,預設啟用
"""

import os
import re
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
    "量產進度": "🚀", "展覽/政策": "📰", "盤後隨筆": "📝", "每日關注": "👀",
    "隨手筆記": "✏️", "經濟數據": "🏦", "重大訊息": "🚨", "其他": "📌",
}
DEFAULT_EMOJI = "📌"

_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _target_id() -> str:
    return os.environ.get("LINE_NOTIFY_TARGET_ID", "").strip()


def _exclude_types() -> set:
    raw = os.environ.get("NOTIFY_EXCLUDE_TYPES", "").strip()
    return {t.strip() for t in raw.split(",") if t.strip()}


# ---- 查 Notion 事件 ----
def query_events(start: datetime.date, end: datetime.date,
                 extra_exclude: set | None = None) -> list[dict]:
    """查「關鍵日期」落在 [start, end](含兩端)的事件,依日期排序。
    回傳 [{'start','end','type','title','url','status'}]。
    extra_exclude:除了全域 NOTIFY_EXCLUDE_TYPES 外,額外要濾掉的事件類型。"""
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
    exclude = _exclude_types() | (extra_exclude or set())
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


def format_daily(events: list[dict], day: datetime.date,
                 label: str = "今日", emoji: str = "☀️") -> str:
    head = f"{emoji} {label}投資行事曆 {day.month}/{day.day}(週{_WEEKDAY_ZH[day.weekday()]})"
    body = "\n".join(_fmt_line(ev) for ev in events) if events else f"(今日無排定事件)"
    return f"{head}\n\n{body}"


def format_earnings_preview(events: list[dict], today: datetime.date) -> str:
    """提前預告區塊(法說會/財報公布),依日期排序,標倒數天數。"""
    lines = []
    for ev in sorted(events, key=lambda e: e["start"]):
        try:
            dd = datetime.date.fromisoformat(ev["start"])
        except ValueError:
            continue
        days = (dd - today).days
        when = "今天" if days == 0 else f"還有{days}天"
        w = _WEEKDAY_ZH[dd.weekday()]
        emoji = TYPE_EMOJI.get(ev["type"], "🎤")
        lines.append(f"{emoji} {dd.month}/{dd.day}(週{w}・{when}) {ev['title']}")
    return "📢 近期預告(法說會/財報)\n" + "\n".join(lines)


# ---- 隨選「列出事件」:本週/下週/指定日期或範圍 ----
_LIST_WEEK_WORDS = {"本週", "本周", "這週", "這周", "本週事件", "本周事件",
                    "這週事件", "這周事件", "本週行事曆", "本周行事曆"}
_LIST_NEXTWEEK_WORDS = {"下週", "下周", "下週事件", "下周事件", "下週行事曆", "下周行事曆"}
_LIST_DATE_RE = re.compile(r"^(?:(\d{4})[/-])?(\d{1,2})[/.](\d{1,2})$")
_LIST_USAGE = (
    "🗓️ 列出用法:\n"
    "• 列出本週 / 列出下週\n"
    "• 列出 7/10(指定某天)\n"
    "• 列出 7/10~7/15(指定範圍)"
)


def is_list_events_command(text: str) -> bool:
    """快速判斷是否為「列出事件」指令(不連網):本週/下週,或「列出…」開頭。"""
    s = (text or "").strip()
    if not s:
        return False
    first = s.splitlines()[0].strip()
    return first in _LIST_WEEK_WORDS or first in _LIST_NEXTWEEK_WORDS or first.startswith("列出")


def _parse_one_date(s: str, today: datetime.date):
    m = _LIST_DATE_RE.match(s.strip())
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return datetime.date(int(y) if y else today.year, int(mo), int(d))
    except ValueError:
        return None


def _this_week(today):
    mon = today - datetime.timedelta(days=today.weekday())
    return mon, mon + datetime.timedelta(days=6)


def _next_week(today):
    mon = today + datetime.timedelta(days=(7 - today.weekday()))
    return mon, mon + datetime.timedelta(days=6)


def _resolve_list_range(arg: str, today: datetime.date):
    """把「列出」後面的參數解析成 (start, end, label);解析不出回 None。"""
    arg = (arg or "").strip()
    if arg in ("", "本週", "本周", "這週", "這周"):
        s, e = _this_week(today)
        return s, e, "本週"
    if arg in ("下週", "下周"):
        s, e = _next_week(today)
        return s, e, "下週"
    parts = re.split(r"\s*(?:~|～|-|到|至)\s*", arg)
    if len(parts) == 2:
        d1, d2 = _parse_one_date(parts[0], today), _parse_one_date(parts[1], today)
        if d1 and d2:
            s, e = (d1, d2) if d1 <= d2 else (d2, d1)
            return s, e, "指定範圍"
    d = _parse_one_date(arg, today)
    if d:
        return d, d, "指定日期"
    return None


def format_event_list(events, start, end, label) -> str:
    single = start == end
    if single:
        head = f"🗓️ {label} {start.month}/{start.day}(週{_WEEKDAY_ZH[start.weekday()]}) 事件"
    else:
        head = (f"🗓️ {label} 事件\n{start.month}/{start.day}(週{_WEEKDAY_ZH[start.weekday()]})"
                f" ~ {end.month}/{end.day}(週{_WEEKDAY_ZH[end.weekday()]})")
    if not events:
        return head + "\n\n(這段期間沒有排定事件)"
    body = "\n".join(_fmt_line(ev, with_date=not single)
                     for ev in sorted(events, key=lambda e: e["start"]))
    return head + "\n\n" + body


def run_list_events(text: str) -> str | None:
    """執行「列出事件」查詢並回傳訊息;非此指令回 None。"""
    s = (text or "").strip()
    first = s.splitlines()[0].strip() if s else ""
    today = datetime.datetime.now(TW_TZ).date()
    if first in _LIST_WEEK_WORDS:
        rng = (*_this_week(today), "本週")
    elif first in _LIST_NEXTWEEK_WORDS:
        rng = (*_next_week(today), "下週")
    elif first.startswith("列出"):
        rng = _resolve_list_range(first[len("列出"):], today)
        if rng is None:
            return _LIST_USAGE
    else:
        return None
    start, end, label = rng
    # 盯盤筆記不算行事曆事件,列表時排除
    events = query_events(start, end, extra_exclude={"每日關注", "隨手筆記", "重大訊息"})
    return format_event_list(events, start, end, label)


# ---- 「查重訊」:查追蹤股已累積的重大訊息(某天/本週/下週/區間)----
_ALERTS_QUERY_PREFIXES = ("查重訊", "重訊查詢", "查重大訊息", "查種訊")
_ALERTS_USAGE = (
    "🚨 查重訊用法:\n"
    "• 查重訊本週 / 查重訊下週\n"
    "• 查重訊 7/10(某天)\n"
    "• 查重訊 7/10~7/15(區間)\n"
    "(只累積本功能上線後、追蹤股的重大訊息)"
)


def is_alerts_query_command(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return s.splitlines()[0].strip().startswith(_ALERTS_QUERY_PREFIXES)


def run_alerts_query(text: str) -> str:
    s = (text or "").strip()
    first = s.splitlines()[0].strip() if s else ""
    arg = first
    for p in _ALERTS_QUERY_PREFIXES:
        if first.startswith(p):
            arg = first[len(p):].strip()
            break
    today = datetime.datetime.now(TW_TZ).date()
    rng = _resolve_list_range(arg, today)  # 空=本週,也吃 下週/日期/區間
    if rng is None:
        return _ALERTS_USAGE
    start, end, label = rng
    events = sorted((e for e in query_events(start, end) if e["type"] == "重大訊息"),
                    key=lambda e: e["start"])
    if start == end:
        head = f"🚨 {start.month}/{start.day} 追蹤股重大訊息"
    else:
        head = f"🚨 {label} 追蹤股重大訊息 {start.month}/{start.day}~{end.month}/{end.day}"
    if not events:
        return f"{head}\n\n(這段期間追蹤股沒有重大訊息)"
    lines = [head, ""]
    for e in events:
        d = e["start"]
        lines.append(f"• {d[5:]} {e['title']}")
    return "\n".join(lines)


# ---- 用量報告(真實數字):LINE 推播則數 / Firecrawl credit / AI token ----
_USAGE_WORDS = {"用量", "用量報告", "查用量", "usage"}


def is_usage_command(text: str) -> bool:
    return (text or "").strip().lower() in _USAGE_WORDS


def fetch_line_usage() -> dict:
    """LINE 本月已用推播則數 / 上限。回 {'type','quota','used'} 或 {'error'}。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        return {"error": "未設定 LINE_CHANNEL_ACCESS_TOKEN"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        q = httpx.get("https://api.line.me/v2/bot/message/quota",
                      headers=headers, timeout=15).json()
        c = httpx.get("https://api.line.me/v2/bot/message/quota/consumption",
                      headers=headers, timeout=15).json()
        return {"type": q.get("type", ""), "quota": q.get("value"), "used": c.get("totalUsage")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def fetch_firecrawl_usage() -> dict:
    """Firecrawl 本期 credit 用量。回 {'remaining','plan','period_end'} 或 {'error'}。"""
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        return {"error": "未設定 FIRECRAWL_API_KEY"}
    base = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/")
    try:
        r = httpx.get(f"{base}/v2/team/credit-usage",
                      headers={"Authorization": f"Bearer {key}"}, timeout=15)
        d = r.json().get("data", {}) or {}
        return {"remaining": d.get("remainingCredits"), "plan": d.get("planCredits"),
                "period_end": d.get("billingPeriodEnd", "")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def run_usage() -> str:
    """組出「用量報告」文字:LINE 推播則數 + Firecrawl credit + AI token(即時抓)。"""
    import news_processor  # 延遲載入,避免模組層循環匯入
    now = datetime.datetime.now(TW_TZ)
    lines = [f"📊 用量報告 {now.month}/{now.day} {now.strftime('%H:%M')}"]

    lu = fetch_line_usage()
    lines += ["", "━ LINE 推播(本月)━"]
    if lu.get("error"):
        lines.append(f"⚠️ 讀取失敗:{lu['error']}")
    elif lu.get("type") == "limited" and lu.get("quota"):
        used, quota = lu.get("used") or 0, lu["quota"]
        pct = round(used / quota * 100) if quota else 0
        lines.append(f"已用 {used} / {quota} 則({pct}%)")
    else:
        lines.append(f"已用 {lu.get('used') or 0} 則(方案 {lu.get('type') or '未知'};免費版上限 200)")

    fu = fetch_firecrawl_usage()
    lines += ["", "━ Firecrawl credit(本期)━"]
    if fu.get("error"):
        lines.append(f"⚠️ 讀取失敗:{fu['error']}")
    else:
        rem, plan = fu.get("remaining"), fu.get("plan")
        if plan and rem is not None:
            lines.append(f"已用 {plan - rem:,} / {plan:,}(剩 {rem:,})")
        elif rem is not None:
            lines.append(f"剩餘 {rem:,} credits")
        else:
            lines.append("(無法解析回傳)")
        if fu.get("period_end"):
            lines.append(f"週期至 {fu['period_end'][:10]}")

    au = news_processor.get_ai_usage()
    lines += ["", f"━ AI token(本月 {au['month']})━"]
    lines.append(f"{au['month_calls']} 次・in {au['month_prompt']:,}・out {au['month_completion']:,}")
    lines.append(f"估算 ~US${au['month_cost']:.3f}(model {au['model']})")
    lines.append(f"累計 {au['all_months']} 個月:{au['all_calls']} 次・~US${au['all_cost']:.3f}")
    if not au["persisted"]:
        lines.append("⚠️ Notion 持久化讀取失敗,AI 數字僅記憶體暫存")

    lines.append("\nℹ️ 三者皆真實用量;AI 用量持久化於 Notion,跨部署/跨月累計。")
    return "\n".join(lines)


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


# ---- 主動推播總開關(守 LINE 免費額度;可用 LINE 指令即時開關,env PUSH_ENABLED 設持久預設)----
# 關閉時:早報/週報/MOPS 都不推(不耗額度);但 MOPS 仍照常掃描、命中仍寫進 Notion,
# 所以「查重訊」查得到、資料不斷。指令回覆(reply)本就免費、不受影響。
_push_enabled = os.environ.get("PUSH_ENABLED", "1").strip() != "0"
_PUSH_OFF_WORDS = {"推播關閉", "關閉推播", "推播暫停", "暫停推播", "推播關", "關推播"}
_PUSH_ON_WORDS = {"推播開啟", "開啟推播", "恢復推播", "推播開", "開推播"}
_PUSH_STATUS_WORDS = {"推播狀態", "推播設定"}


def is_push_enabled() -> bool:
    return _push_enabled


def set_push_enabled(on: bool) -> None:
    global _push_enabled
    _push_enabled = on


def is_push_toggle_command(text: str) -> str | None:
    """整段字比對;回 'off'/'on'/'status' 或 None。"""
    s = (text or "").strip()
    if s in _PUSH_OFF_WORDS:
        return "off"
    if s in _PUSH_ON_WORDS:
        return "on"
    if s in _PUSH_STATUS_WORDS:
        return "status"
    return None


def run_push_toggle(action: str) -> str:
    if action == "off":
        set_push_enabled(False)
        return (
            "🔕 已暫停所有主動推播(早報/週報/MOPS 警示),不再消耗 LINE 額度。\n"
            "背景仍在掃描記錄——隨時打「早報」「下週」「查重訊今天」都查得到(免費)。\n"
            "要恢復打「推播開啟」。"
        )
    if action == "on":
        set_push_enabled(True)
        return "🔔 已恢復主動推播(早報/週報/MOPS 警示)。"
    return f"主動推播目前:{'開啟中 🔔' if _push_enabled else '暫停中 🔕'}\n(關:推播關閉／開:推播開啟)"


def is_today_brief_command(text: str) -> bool:
    return (text or "").strip() in {"早報", "今日", "今日行事曆", "今日重點", "當日行事曆"}


def run_today_brief() -> str:
    """打「早報」即時叫出今日行事曆+法說會預告(等同 08:30 推播內容,但用 reply 不計額度)。"""
    msg = notify_daily(dry_run=True)
    if not msg:
        today = datetime.datetime.now(TW_TZ).date()
        return f"🌅 今日行事曆 {today.month}/{today.day}\n\n(今天沒有排定事件,近日也沒有法說會預告)"
    return msg


# ---- 推播到 LINE ----
def push(text: str, target_id: str = "") -> None:
    if not _push_enabled:
        logger.info("主動推播已暫停(PUSH_ENABLED/指令),略過。")
        return
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


# 提前預告:當天 + 前幾天都通知(預設 2 = 當天+前2天共 3 個早上)
# 註:變數名叫 EARNINGS_LEAD_DAYS,但預告對象由 _PREVIEW_TYPES 決定(法說會 + 財報公布)
EARNINGS_LEAD_DAYS = int(os.environ.get("EARNINGS_LEAD_DAYS", "2"))
_PREVIEW_TYPES = {"法說會", "財報公布"}


def format_notes(notes: list[dict], day: datetime.date) -> str:
    """今日隨筆彙整區塊(依時間排序,標記錄時間)。"""
    head = f"📝 今日隨筆彙整 {day.month}/{day.day}"
    body = "\n".join(
        f"・{n['time'] + ' ' if n.get('time') else ''}{n['title']}" for n in notes
    )
    return f"{head}\n{body}"


# ---- 「股訊」查個股已存新聞/事件(唯讀 reply,不計額度)----
# 分組:新聞報告 / 事件(法說會、財報等) / 重大訊息;盯盤與隨筆類不列入。
_STOCKINFO_NEWS = {"個股新聞", "產業新聞", "個股產業報告"}
_STOCKINFO_ALERT = {"重大訊息"}
_STOCKINFO_SKIP = {"每日關注", "隨手筆記", "盤後隨筆"}
_STOCKINFO_EACH = 5  # 每組最多列幾則


def is_stock_info_command(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith("股訊 ") or t == "股訊"


def _stockinfo_group(event_type: str) -> str:
    if event_type in _STOCKINFO_NEWS:
        return "news"
    if event_type in _STOCKINFO_ALERT:
        return "alert"
    if event_type in _STOCKINFO_SKIP:
        return "skip"
    return "event"


def _strip_label_prefix(title: str, label: str) -> str:
    """重訊標題常以「2303 聯電 …」開頭,列表已標明個股,去掉重複前綴。"""
    for pre in (label + " ", label):
        if pre and title.startswith(pre):
            return title[len(pre):].strip()
    return title


def run_stock_info(text: str) -> str:
    """「股訊 2303」→ 分組列表(帶編號);「股訊 2303 2」→ 展開第 2 則全文。
    只打「股訊」或查無關鍵字 → 回空字串(不回覆,避免誤觸)。"""
    arg = (text or "").strip()[len("股訊"):].strip()
    if not arg:
        return ""
    parts = arg.split()
    key = parts[0]
    idx = 0
    if len(parts) > 1 and parts[1].isdigit():
        idx = int(parts[1])

    stock = nt.find_stock_page(key, key)
    if not stock:
        return f"📄 個股主表查無「{key}」,可能還沒建檔"
    label = stock["label"]
    events = nt.list_events_by_stock(stock["id"])
    items = [e for e in events if _stockinfo_group(e["type"]) != "skip"]
    if not items:
        return f"📄 {label} 目前沒有已存訊息"

    # 依組別重排(新聞→事件→重訊),編號跨組連續,讓「股訊 代號 N」可直接指定
    order = {"news": 0, "event": 1, "alert": 2}
    items.sort(key=lambda e: (order[_stockinfo_group(e["type"])], ), reverse=False)

    if idx:  # 展開單則
        if idx > len(items):
            return f"📄 {label} 只有 {len(items)} 則,查無第 {idx} 則"
        ev = items[idx - 1]
        d = ev["date"][:10] or "(無日期)"
        head = f"📄 {label}｜[{idx}] {ev['type']}\n{d} {_strip_label_prefix(ev['title'], label)}"
        body = ev["note"] or "(這則沒有存摘要內容)"
        tail = f"\n\n🔗 {ev['link']}" if ev["link"] else ""
        return f"{head}\n{'─' * 13}\n{body[:3500]}{tail}"

    lines = [f"📄 {label}｜最近訊息"]
    n = 0
    for gkey, head in (("news", "📰 新聞/報告"), ("event", "📅 事件"), ("alert", "🚨 重大訊息")):
        group = [e for e in items if _stockinfo_group(e["type"]) == gkey]
        if not group:
            continue
        lines.append("")
        lines.append(head)
        for ev in group[:_STOCKINFO_EACH]:
            n = items.index(ev) + 1
            md = ev["date"][5:10].replace("-", "/") if len(ev["date"]) >= 10 else ev["date"]
            title = _strip_label_prefix(ev["title"], label)
            mark = "" if gkey == "alert" else f"[{ev['type']}] "
            lines.append(f"[{n}] {md} {mark}{title[:24]}")
        if len(group) > _STOCKINFO_EACH:
            lines.append(f"…另有 {len(group) - _STOCKINFO_EACH} 則")
    lines.append("")
    lines.append(f"看內文:股訊 {key} 編號")
    return "\n".join(lines)


# ---- 排程任務 ----
def notify_daily(dry_run: bool = False) -> str | None:
    today = datetime.datetime.now(TW_TZ).date()
    # 隨手筆記只在 21:00 彙整、重大訊息只在「查重訊」看,都不在早上出現
    events = query_events(today, today, extra_exclude={"隨手筆記", "重大訊息"})

    # 法說會提前預告:未來 1~EARNINGS_LEAD_DAYS 天內的法說會(僅預告類型)
    preview = []
    if EARNINGS_LEAD_DAYS > 0:
        ahead = query_events(today + datetime.timedelta(days=1),
                             today + datetime.timedelta(days=EARNINGS_LEAD_DAYS))
        preview = [e for e in ahead if e["type"] in _PREVIEW_TYPES]

    if not events and not preview:
        logger.info("今日(%s)無事件、近日無法說會,不推播。", today)
        return None

    parts = [format_daily(events, today)]
    if preview:
        parts.append(format_earnings_preview(preview, today))
    msg = "\n\n".join(parts)
    if dry_run:
        return msg
    push(msg)
    return msg


def notify_tomorrow(dry_run: bool = False) -> str | None:
    today = datetime.datetime.now(TW_TZ).date()
    tomorrow = today + datetime.timedelta(days=1)
    # 明日事件(排除盯盤筆記與隨手筆記);今日隨筆另段彙整
    events = query_events(tomorrow, tomorrow, extra_exclude={"每日關注", "隨手筆記", "重大訊息"})
    notes = nt.list_notes_by_date(today.isoformat(), "隨手筆記")
    if not events and not notes:
        logger.info("明日(%s)無事件、今日無隨筆,不推播。", tomorrow)
        return None
    parts = [format_daily(events, tomorrow, label="明日", emoji="🌙")]
    if notes:
        parts.append(format_notes(notes, today))
    msg = "\n\n".join(parts)
    if dry_run:
        return msg
    push(msg)
    return msg


def notify_weekly(dry_run: bool = False) -> str | None:
    today = datetime.datetime.now(TW_TZ).date()
    # 下週一 = 今天之後最近的週一;下週日 = 下週一 + 6
    next_mon = today + datetime.timedelta(days=(7 - today.weekday()))
    next_sun = next_mon + datetime.timedelta(days=6)
    # 週報只放真實行事曆事件,不含盯盤筆記/隨手筆記
    events = query_events(next_mon, next_sun, extra_exclude={"每日關注", "隨手筆記", "重大訊息"})
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
    t_h, t_m = _parse_hhmm(os.environ.get("NOTIFY_TOMORROW_TIME", "21:00"), 21, 0)
    w_h, w_m = _parse_hhmm(os.environ.get("NOTIFY_WEEKLY_TIME", "21:00"), 21, 0)
    w_dow = int(os.environ.get("NOTIFY_WEEKLY_DOW", "6"))  # 0=一…6=日
    f_h, f_m = _parse_hhmm(os.environ.get("FETCH_EARNINGS_TIME", "06:30"), 6, 30)
    e_h, e_m = _parse_hhmm(os.environ.get("FETCH_ECON_TIME", "06:20"), 6, 20)

    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(
        _safe(notify_daily), CronTrigger(hour=d_h, minute=d_m, timezone=tz),
        id="daily", replace_existing=True,
    )
    # 每日 21:00「明日事件＋今日隨筆彙整」推播:2026-07-07 起預設關閉以節省 LINE push 則數
    # (要恢復設 ENABLE_TOMORROW_NOTIFY=1;隨筆仍可隨時打「今日隨筆」用 reply 查,不計則數)
    tomorrow_log = ""
    if os.environ.get("ENABLE_TOMORROW_NOTIFY", "0").strip() != "0":
        sched.add_job(
            _safe(notify_tomorrow), CronTrigger(hour=t_h, minute=t_m, timezone=tz),
            id="tomorrow", replace_existing=True,
        )
        tomorrow_log = f"、每日 {t_h:02d}:{t_m:02d} 推明日"
    sched.add_job(
        _safe(notify_weekly), CronTrigger(day_of_week=w_dow, hour=w_h, minute=w_m, timezone=tz),
        id="weekly", replace_existing=True,
    )
    # 每天 06:30(推播前)自動抓 money-link 法說會,更新追蹤股的時程
    try:
        import fetchers
        sched.add_job(
            _safe(fetchers.sync_earnings_calls),
            CronTrigger(hour=f_h, minute=f_m, timezone=tz),
            id="fetch_earnings", replace_existing=True,
        )
        fetch_log = f"、每日 {f_h:02d}:{f_m:02d} 抓法說會"
        # 每天 06:20 抓 MacroMicro 美國經濟事件(需 FIRECRAWL_API_KEY,沒設會自動略過)
        if fetchers.econ_enabled():
            sched.add_job(
                _safe(fetchers.sync_econ_events),
                CronTrigger(hour=e_h, minute=e_m, timezone=tz),
                id="fetch_econ", replace_existing=True,
            )
            fetch_log += f"、每日 {e_h:02d}:{e_m:02d} 抓美國經濟事件"
        # 每 2 小時監看 MOPS 即時重大訊息,追蹤股命中就把該窗全部命中「合併成一則」推播
        # (降低 LINE push 則數以守住免費 200 則/月;每輪的多筆命中本來就合併成一則)
        mops_min = os.environ.get("MOPS_CHECK_MINUTES", "0")
        mops_hours = os.environ.get("MOPS_CHECK_HOURS", "6-22/2")
        sched.add_job(
            _safe(fetchers.check_mops_alerts),
            CronTrigger(hour=mops_hours, minute=mops_min, timezone=tz),
            id="mops_alerts", replace_existing=True,
        )
        fetch_log += "、每 2 小時監看重大訊息"
    except Exception:  # noqa: BLE001
        logger.exception("fetchers 掛載失敗,法說會自動抓取不啟用")
        fetch_log = ""

    sched.start()
    _scheduler = sched
    logger.info(
        "行事曆排程已啟動:每日 %02d:%02d 推當日%s、"
        "每週 %s %02d:%02d 推下週%s(台灣時間)。",
        d_h, d_m, tomorrow_log, _WEEKDAY_ZH[w_dow], w_h, w_m, fetch_log,
    )


def _parse_hhmm(s: str, dh: int, dm: int) -> tuple[int, int]:
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return dh, dm


# ---- 排程失敗告警(靜默失敗保護)----
# 背景:_safe() 會吞掉所有例外、只寫 log,任務失敗時完全不會被發現。
# 2026-07 踩過:Firecrawl 額度用盡 → 每日 06:20 的 MacroMicro 經濟事件抓取連續失敗多天,
# 機器人照跑、其他排程正常、Notion 就是沒資料進來,只有翻 Zeabur log 才看得到。
# MacroMicro 有 Cloudflare,fetchers 明確不能用純 httpx fallback,所以這條沒有備援路徑。
_JOB_FAILS: dict[str, int] = {}
# 連續失敗幾次才告警(預設 2;避免單次網路抖動就吵人)
JOB_ALERT_AFTER = int(os.environ.get("JOB_ALERT_AFTER", "2"))
# 持續失敗時每隔幾次再提醒一次(預設 12;避免洗版吃掉 LINE 免費 200 則/月額度)
JOB_ALERT_REPEAT = int(os.environ.get("JOB_ALERT_REPEAT", "12"))


def _alert(text: str) -> None:
    """告警推播;告警本身失敗只寫 log,絕不讓它拖垮排程。"""
    try:
        push(text)
    except Exception:  # noqa: BLE001
        logger.exception("告警推播失敗(原訊息:%s)", text[:80])


def _safe(fn):
    name = getattr(fn, "__name__", str(fn))

    def wrapper():
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("排程任務執行失敗:%s", name)
            n = _JOB_FAILS[name] = _JOB_FAILS.get(name, 0) + 1
            # 剛好到門檻時告警一次;之後每 JOB_ALERT_REPEAT 次再提醒,不每次都推
            if n == JOB_ALERT_AFTER or (n > JOB_ALERT_AFTER and n % JOB_ALERT_REPEAT == 0):
                _alert(
                    "⚠️ 排程異常\n"
                    f"任務:{name}\n"
                    f"已連續失敗 {n} 次\n"
                    "常見原因:Firecrawl 額度用盡 / 來源網站改版 / API key 失效\n"
                    "查原因:Zeabur log。查額度:打「用量」"
                )
        else:
            # 從失敗中恢復也要通知,否則不知道問題何時解決(或以為還壞著)
            prev = _JOB_FAILS.get(name, 0)
            if prev >= JOB_ALERT_AFTER:
                _alert(f"✅ 排程已恢復:{name}(先前連續失敗 {prev} 次)")
            _JOB_FAILS[name] = 0

    wrapper.__name__ = name
    return wrapper


# ---- CLI:本機測試 ----
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv

    if mode == "daily":
        out = notify_daily(dry_run=dry)
    elif mode == "tomorrow":
        out = notify_tomorrow(dry_run=dry)
    elif mode == "weekly":
        out = notify_weekly(dry_run=dry)
    else:
        print("用法:python calendar_notify.py [daily|tomorrow|weekly] [--dry]")
        sys.exit(1)

    if dry:
        print("=== DRY RUN(不推播,僅顯示訊息)===")
        print(out or "(無事件,不會推播)")
    else:
        print("已執行" + mode + (",無事件未推播" if out is None else ",已推播"))

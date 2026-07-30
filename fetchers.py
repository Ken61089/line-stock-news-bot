"""
公開資訊自動抓取 → 寫進 Notion 時程庫。

來源(合併去重):
  1. 富聯網(money-link)國內法說會時間表(靜態頁,big5,最全,含地點/說明)
  2. TDCC 集保 IR 平台法說會列表(補 money-link 漏的,附法說會簡報 PDF)
只抓「Notion 個股主表裡追蹤的股票」、未來日期、含所有出席(自辦法說會 +
出席券商論壇/座談會),寫成時程庫「法說會」事件,並依 (個股, 日期) 去重。

由 calendar_notify 的排程每日觸發;也可用 LINE 指令「更新法說會」手動觸發。
"""

import os
import re
import time
import logging
import datetime
import statistics
import collections

import httpx

import notion_timeline as nt

logger = logging.getLogger("line-news-bot.fetchers")

MONEY_LINK_URL = "https://www.money-link.com.tw/stxba/imwcontent0.asp?page=INVC1&ID=INVC1"
TDCC_URL = "https://irplatform.tdcc.com.tw/ir/zh/event/list"
TW_TZ = nt.TW_TZ

# MacroMicro 全球財經行事曆(有 Cloudflare 防爬 → 必須經 Firecrawl 抓,不能純 httpx)
MACROMICRO_URL = "https://www.macromicro.me/calendar"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_BASE_URL = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/")
_ECON_TYPE = "經濟數據"
_ECON_HORIZON_DAYS = int(os.environ.get("ECON_HORIZON_DAYS", "60"))  # 抓當月+下月,60天確保下月完整涵蓋
# 把 MacroMicro 行事曆「區域」下拉選成美國(option value=us),觸發只顯示美國事件
_MM_SELECT_US_JS = (
    "document.querySelectorAll('select').forEach(function(s){"
    "if(Array.prototype.some.call(s.options,function(o){return o.value==='us';}))"
    "{s.value='us';s.dispatchEvent(new Event('change',{bubbles:true}));}});"
)
# 點行事曆工具列「下一月」箭頭(FontAwesome fa-chevron-right;左箭頭是上一月)。
# 用來翻到下個月抓資料——月底時當月月曆已看不到下月的 CPI/FOMC 等,靠翻月補齊。
_MM_NEXT_MONTH_JS = (
    "(function(){var i=document.querySelector('.fa-chevron-right');"
    "if(i){var el=i.closest('button,a,[role=button]')||i.parentElement||i;el.click();}})()"
)
# 抓幾個月(當月起算):預設 2 = 當月 + 下月,可用 env ECON_MONTHS 調整
_ECON_MONTHS = max(1, int(os.environ.get("ECON_MONTHS", "2")))
_MM_MONTH_RE = re.compile(r"####\s*(\d{4})\s*年\s*(\d{1,2})\s*月")
_MM_DAY_RE = re.compile(r"\*\*(\d{1,2})(?:[（(]週.[)）])?\*\*")
_MM_EVENT_RE = re.compile(r"\[([^\]]+?)\]\((https://www\.macromicro\.me/[^)]+)\)")

# 解析 money-link 每一列法說會(日期西元年在 class='bc',代號在 listcode)
_ROW_RE = re.compile(
    r"<span class='bc'>(\d{4})</span>/(\d{2})/(\d{2})</td>\s*"
    r"<td[^>]*><span class=\"listcode\"[^>]*>(\d+)</span></td>\s*"
    r"<td[^>]*>([^<]*)</td>\s*"   # 公司名稱
    r"<td[^>]*>([^<]*)</td>\s*"   # 時間
    r"<td[^>]*>([^<]*)</td>\s*"   # 地點
    r"<td[^>]*>([^<]*)</td>",     # 相關訊息
    re.S,
)

# 解析 TDCC IR 平台每個 active-box(代號/名稱、日期時間、類型、選配簡報 PDF)
_TDCC_RE = re.compile(
    r"<h3>(\d+)\s+([^<]+)</h3>\s*"
    r"<time>(\d{4})/(\d{2})/(\d{2})\s+([\d:]+)</time>\s*"
    r"<span class=\"meeting[^\"]*\">([^<]+)</span>"
    r"(?:\s*<a[^>]*href=\"([^\"]+)\")?",
    re.S,
)


_EARNINGS_TYPE = "法說會"
_HORIZON_DAYS = 120  # 只抓未來 120 天內的法說會


def fetch_moneylink_rows() -> list[dict]:
    """抓 money-link 法說會表,回傳 [{date, code, name, time, place, message, link}]。"""
    r = httpx.get(MONEY_LINK_URL, timeout=30)
    r.raise_for_status()
    html = r.content.decode("big5", errors="ignore")
    rows = []
    for m in _ROW_RE.finditer(html):
        y, mo, d, code, name, tm, place, msg = m.groups()
        rows.append({
            "date": f"{y}-{mo}-{d}",
            "code": code.strip(),
            "name": re.sub(r"\s+", "", name).strip(),
            "time": tm.strip(),
            "place": place.strip(),
            "message": msg.strip(),
            "link": "",
        })
    return rows


def fetch_tdcc_rows() -> list[dict]:
    """抓 TDCC IR 平台法說會列表(只取法說會類),回傳同 money-link 的欄位格式。"""
    r = httpx.get(TDCC_URL, timeout=30, follow_redirects=True)
    r.raise_for_status()
    rows = []
    for m in _TDCC_RE.finditer(r.text):
        code, name, y, mo, d, tm, mtype, pdf = m.groups()
        if "法說" not in mtype and "法人說明" not in mtype:
            continue  # 只要法說會,略過其他活動
        rows.append({
            "date": f"{y}-{mo}-{d}",
            "code": code.strip(),
            "name": re.sub(r"\s+", "", name).strip(),
            "time": tm.strip(),
            "place": "",
            "message": mtype.strip(),
            "link": (pdf or "").strip(),
        })
    return rows


def fetch_earnings_rows() -> list[dict]:
    """合併 money-link + TDCC,依 (代號, 日期) 去重。money-link 優先(欄位較全),
    但缺簡報連結時用 TDCC 的補上;TDCC 獨有的(money-link 沒列)也一併納入。"""
    merged: dict[tuple, dict] = {}
    for row in _safe_fetch(fetch_moneylink_rows, "money-link") + _safe_fetch(fetch_tdcc_rows, "TDCC"):
        key = (row["code"], row["date"])
        if key not in merged:
            merged[key] = row
        else:
            # 已有(通常是 money-link):只補空的簡報連結
            if not merged[key].get("link") and row.get("link"):
                merged[key]["link"] = row["link"]
    return list(merged.values())


def _safe_fetch(fn, label: str) -> list[dict]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        logger.warning("抓取 %s 失敗(略過此來源):%s", label, e)
        return []


def _existing_earnings(since: datetime.date) -> set:
    """時程庫既有的「法說會」事件(關鍵日期 >= since),回傳 {(關聯個股id, 日期)} 供去重。
    同時涵蓋手動輸入與自動抓取的,避免重複建立。"""
    existing = set()
    cursor = None
    while True:
        payload = {
            "filter": {
                "and": [
                    {"property": "事件類型", "select": {"equals": _EARNINGS_TYPE}},
                    {"property": "關鍵日期", "date": {"on_or_after": since.isoformat()}},
                ]
            },
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = nt._post(f"/data_sources/{nt.TIMELINE_DS}/query", payload)
        for pg in data.get("results", []):
            props = pg.get("properties", {})
            date = ((props.get("關鍵日期", {}).get("date") or {}).get("start") or "")[:10]
            rel = props.get("🔗 關聯個股", {}).get("relation", [])
            sid = rel[0]["id"] if rel else ""
            existing.add((sid, date))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return existing


def sync_earnings_calls(dry_run: bool = False) -> dict:
    """抓 money-link 法說會 → 只留「追蹤個股 + 未來(120天內)」→ 依(個股,日期)去重 → 寫進時程庫。
    回傳 {'matched','added','skipped','added_items'}。dry_run=True 只回報不寫入。"""
    today = datetime.datetime.now(TW_TZ).date()
    horizon = (today + datetime.timedelta(days=_HORIZON_DAYS)).isoformat()
    today_iso = today.isoformat()

    rows = [r for r in fetch_earnings_rows() if today_iso <= r["date"] <= horizon]

    by_code = {s["code"]: s for s in nt.list_stocks() if s["code"]}
    existing = _existing_earnings(today)

    matched, added, skipped, added_items = 0, 0, 0, []
    for r in rows:
        stock = by_code.get(r["code"])
        if not stock:
            continue  # 非追蹤個股,略過
        matched += 1
        if (stock["id"], r["date"]) in existing:
            skipped += 1
            continue

        note = "｜".join(x for x in (r["message"], r["time"], r["place"]) if x)[:1900]
        added_items.append(f"{r['date']} {r['name']} — {r['message'][:20]}")
        if dry_run:
            added += 1
            continue
        try:
            nt.add_event(
                title=f"{r['name']} 法說會",
                date_start=r["date"],
                event_type=_EARNINGS_TYPE,
                stock_page_id=stock["id"],
                concept_ids=stock["concept_ids"],
                note=note,
                source="自動抓取",
                status="預定",
                link=r.get("link", ""),
            )
            existing.add((stock["id"], r["date"]))  # 同批內也去重
            added += 1
        except nt.NotionError as e:
            logger.warning("寫入法說會失敗(%s %s):%s", r["code"], r["date"], e)

    logger.info("法說會同步:比對到 %d 筆追蹤股,新增 %d,略過(已存在)%d", matched, added, skipped)
    return {"matched": matched, "added": added, "skipped": skipped, "added_items": added_items}


# ==========================================================
# MacroMicro 美國經濟事件(經 Firecrawl 抓,繞過 Cloudflare)
# ==========================================================
def econ_enabled() -> bool:
    return bool(FIRECRAWL_API_KEY)


def fetch_macromicro_md(month_offset: int = 0) -> str:
    """經 Firecrawl 抓 MacroMicro 行事曆,回傳 markdown(MacroMicro 有 Cloudflare,純 httpx 會 403)。
    month_offset:0=當月,1=下個月…(靠點工具列「下一月」箭頭翻頁)。"""
    if not FIRECRAWL_API_KEY:
        raise RuntimeError("未設定 FIRECRAWL_API_KEY,無法抓 MacroMicro(Cloudflare 需 Firecrawl)")
    # location=台灣 → MacroMicro 直接渲染成台灣時間/日期(不必自己換時區,和使用者看到的一致);
    # 用 JS 把「區域」下拉選成美國(option value=us)→ 只留美國事件,不被亞洲事件擠掉摺疊區。
    actions = [
        {"type": "executeJavascript", "script": _MM_SELECT_US_JS},
        {"type": "wait", "milliseconds": 3000},
    ]
    for _ in range(max(0, month_offset)):  # 逐月點「下一月」,每次等月曆重繪
        actions.append({"type": "executeJavascript", "script": _MM_NEXT_MONTH_JS})
        actions.append({"type": "wait", "milliseconds": 3500})
    r = httpx.post(
        f"{FIRECRAWL_BASE_URL}/v2/scrape",
        headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
        json={
            "url": MACROMICRO_URL,
            "formats": ["markdown"],
            "waitFor": 5000,
            "location": {"country": "TW", "languages": ["zh-TW"]},
            "actions": actions,
        },
        timeout=120,
    )
    r.raise_for_status()
    md = ((r.json().get("data") or {}).get("markdown") or "").strip()
    if not md:
        raise RuntimeError("Firecrawl 回傳空內容")
    return md


def parse_macromicro_us(md: str) -> list[dict]:
    """從 MacroMicro 月曆 markdown 解析「美國」經濟事件,回傳 [{date, name, time, url}]。
    處理月曆邊界(上月尾日/下月頭日)與跨月;只留名稱含「美國」或連結含 /us- 的事件。"""
    mm = _MM_MONTH_RE.search(md)
    if not mm:
        return []
    year, month = int(mm.group(1)), int(mm.group(2))
    tokens = [(m.start(), "day", int(m.group(1))) for m in _MM_DAY_RE.finditer(md)]
    tokens += [(m.start(), "event", (m.group(1), m.group(2))) for m in _MM_EVENT_RE.finditer(md)]
    tokens.sort(key=lambda x: x[0])
    day_nums = [t[2] for t in tokens if t[1] == "day"]
    if not day_nums:
        return []
    cm, cy = month, year
    if day_nums[0] > 20:  # 月曆先顯示上月尾幾天 → 起始月往前一個月
        cm, cy = (12, cy - 1) if cm == 1 else (cm - 1, cy)

    out, cur, last = [], None, 0
    for _pos, kind, val in tokens:
        if kind == "day":
            d = val
            if d < last:  # 日期倒退 → 跨到下個月
                cm, cy = (1, cy + 1) if cm == 12 else (cm + 1, cy)
            last = d
            try:
                cur = datetime.date(cy, cm, d)
            except ValueError:
                cur = None
        elif cur is not None:
            name_raw, url = val
            clean = re.sub(r"\s+", " ", re.sub(r"\\+", " ", name_raw)).strip()
            tmatch = re.search(r"(\d{1,2}:\d{2})\s*$", clean)
            time_s = tmatch.group(1) if tmatch else ""
            name = clean[:tmatch.start()].strip() if tmatch else clean
            if ("美國" in name) or ("/us-" in url):
                out.append({"date": cur.isoformat(), "name": name, "time": time_s, "url": url})
    return out


def _existing_econ(since: datetime.date) -> set:
    """時程庫既有的「經濟數據」事件(關鍵日期 >= since),回傳 {(標題, 日期)} 供去重。"""
    existing = set()
    cursor = None
    while True:
        payload = {
            "filter": {"and": [
                {"property": "事件類型", "select": {"equals": _ECON_TYPE}},
                {"property": "關鍵日期", "date": {"on_or_after": since.isoformat()}},
            ]},
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = nt._post(f"/data_sources/{nt.TIMELINE_DS}/query", payload)
        for pg in data.get("results", []):
            props = pg.get("properties", {})
            title = "".join(t.get("plain_text", "") for t in props.get("Name", {}).get("title", [])).strip()
            date = ((props.get("關鍵日期", {}).get("date") or {}).get("start") or "")[:10]
            existing.add((title, date))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return existing


def sync_econ_events(dry_run: bool = False) -> dict:
    """抓 MacroMicro → 只留「美國事件 + 未來(45天內)」→ 依(標題,日期)去重 → 寫進時程庫「經濟數據」。
    回傳 {'found','added','skipped','added_items'}。"""
    today = datetime.datetime.now(TW_TZ).date()
    horizon = (today + datetime.timedelta(days=_ECON_HORIZON_DAYS)).isoformat()
    today_iso = today.isoformat()

    # 抓當月 + 下個月(月底時當月月曆看不到下月的 CPI/FOMC,靠翻月補齊);合併去重
    raw = []
    for off in range(_ECON_MONTHS):
        try:
            raw += parse_macromicro_us(fetch_macromicro_md(off))
        except Exception as e:  # noqa: BLE001 — 某一月抓失敗不影響其他月
            logger.warning("MacroMicro 第 %d 個月抓取失敗:%s", off, e)
    seen, rows = set(), []
    for r in raw:
        if not (today_iso <= r["date"] <= horizon):
            continue
        key = (r["name"], r["date"])  # 跨月邊界日可能重複,依(標題,日期)去重
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    rows.sort(key=lambda r: (r["date"], r["name"]))
    existing = _existing_econ(today)

    added, skipped, items = 0, 0, []
    for r in rows:
        if (r["name"], r["date"]) in existing:
            skipped += 1
            continue
        items.append(f"{r['date']} {r['name']}" + (f"（{r['time']}）" if r["time"] else ""))
        if dry_run:
            added += 1
            continue
        try:
            nt.add_event(
                title=r["name"],
                date_start=r["date"],
                event_type=_ECON_TYPE,
                note=(f"台灣時間 {r['time']}" if r["time"] else ""),
                source="自動抓取",
                status="預定",
                link=r["url"],
            )
            existing.add((r["name"], r["date"]))
            added += 1
        except nt.NotionError as e:
            logger.warning("寫入經濟數據失敗(%s %s):%s", r["name"], r["date"], e)

    logger.info("經濟數據同步:美國事件 %d 筆(未來%d天),新增 %d,略過 %d", len(rows), _ECON_HORIZON_DAYS, added, skipped)
    return {"found": len(rows), "added": added, "skipped": skipped, "added_items": items}


# ==========================================================
# MOPS 即時重大訊息監看 → 追蹤股命中就 LINE 警示
# ==========================================================
MOPS_URL = "https://mopsov.twse.com.tw/mops/web/t05sr01_1"
_MOPS_ROW_RE = re.compile(
    r"<td[^>]*>(\d{3,6})</td>\s*<td[^>]*>([^<]+)</td>\s*"
    r"<td[^>]*>(\d{3}/\d{2}/\d{2})</td>\s*<td[^>]*>([\d:]+)</td>\s*"
    r"<td[^>]*>([^<]*)</td>\s*<td>\s*<input[^>]*?skey\.value='([^']+)'",
    re.S,
)
_mops_seen: set = set()
_mops_baselined = False


def fetch_mops_announcements() -> list[dict]:
    """抓 MOPS 即時重大訊息(今天全部公司),回傳 [{code,name,date,time,subject,skey}]。"""
    r = httpx.get(MOPS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    html = r.content.decode("utf-8", "ignore")
    out = []
    for code, name, date, tm, subj, skey in _MOPS_ROW_RE.findall(html):
        out.append({
            "code": code.strip(),
            "name": re.sub(r"\s+", "", name).strip(),
            "date": date.strip(),
            "time": tm.strip(),
            "subject": re.sub(r"\s+", " ", subj).strip(),
            "skey": skey.strip(),
        })
    return out


def _push_line(text: str) -> None:
    try:  # 主動推播總開關關閉時只跳過推播;MOPS 掃描/寫 Notion 照常(查重訊仍查得到)
        import calendar_notify
        if not calendar_notify.is_push_enabled():
            logger.info("主動推播已暫停,MOPS 命中改只記 Notion、略過推播")
            return
    except Exception:  # noqa: BLE001
        pass
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    target = os.environ.get("LINE_NOTIFY_TARGET_ID", "").strip()
    if not (token and target):
        logger.warning("未設定 LINE token/target,略過重訊警示推播")
        return
    resp = httpx.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": target, "messages": [{"type": "text", "text": text[:4900]}]},
        timeout=20,
    )
    if resp.status_code >= 300:
        logger.warning("重訊警示推播失敗 %s: %s", resp.status_code, resp.text[:200])


def check_mops_alerts() -> dict:
    """輪詢 MOPS 即時重訊:第一次建基準(不推),之後只對「新出現且屬追蹤股」的重訊推警示。
    回傳 {'total','new','hits'}。"""
    global _mops_baselined
    rows = fetch_mops_announcements()
    fresh = [r for r in rows if r["skey"] not in _mops_seen]
    for r in rows:
        _mops_seen.add(r["skey"])
    if len(_mops_seen) > 5000:  # 界線,避免長跑無限膨脹(skey 每天重置)
        _mops_seen.clear()
        _mops_seen.update(r["skey"] for r in rows)

    if not _mops_baselined:
        _mops_baselined = True
        logger.info("MOPS 重訊基準建立(%d 則),之後只推新的", len(rows))
        return {"total": len(rows), "new": 0, "hits": 0}

    if not fresh:
        return {"total": len(rows), "new": 0, "hits": 0}

    by_code = {s["code"]: s for s in nt.list_stocks() if s["code"]}
    hits = [r for r in fresh if r["code"] in by_code]
    if hits:
        lines = ["🚨 重大訊息(追蹤股)"]
        for r in hits:
            lines.append(f"\n{r['code']} {r['name']}  {r['time']}\n{r['subject'][:180]}")
        # MOPS 命中預設「只記 Notion、不推 LINE」(省 LINE 額度;用「查重訊」pull 補看)。
        # 要恢復命中即時推播:設 env MOPS_ALERT_PUSH=1
        if os.environ.get("MOPS_ALERT_PUSH", "0").strip() != "0":
            _push_line("\n".join(lines))
        else:
            logger.info("MOPS 命中 %d 檔,MOPS_ALERT_PUSH 關閉,只記 Notion 不推播", len(hits))
        # 同時存進 Notion(事件類型「重大訊息」)→ 之後可用「查重訊」查區間
        for r in hits:
            st = by_code[r["code"]]
            try:
                nt.add_event(
                    title=f"{r['code']} {r['name']} {r['subject'][:60]}",
                    date_start=_roc_to_iso(r["date"]),
                    event_type="重大訊息",
                    stock_page_id=st["id"],
                    concept_ids=st["concept_ids"],
                    note=f"{r['time']}｜{r['subject']}"[:1900],
                    source="自動抓取",
                    status="已發生",
                )
            except nt.NotionError as e:
                logger.warning("寫入重大訊息失敗(%s):%s", r["code"], e)
    logger.info("MOPS 重訊:新 %d 則,命中追蹤股 %d 則", len(fresh), len(hits))
    return {"total": len(rows), "new": len(fresh), "hits": len(hits)}


def _roc_to_iso(roc: str) -> str:
    """民國日期 115/07/05 → 西元 2026-07-05。"""
    try:
        y, m, d = roc.split("/")
        return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return datetime.datetime.now(TW_TZ).date().isoformat()


# ==== 融資餘額增減查詢(上市 TWSE + 上櫃 TPEx,查詢式、reply、不推播)====
_MARGIN_UA = {"User-Agent": "Mozilla/5.0"}


def _margin_num(s) -> int:
    """'545,533,645' → 545533645;空值→0。"""
    return int(str(s).replace(",", "").strip() or 0)


class MarginBusyError(RuntimeError):
    """上游(TWSE/TPEx)限流或暫時無回應,與「該日真的沒資料」不同。"""


def _margin_json(url: str, tries: int = 3, headers: dict | None = None):
    """抓 JSON(物件或陣列),容忍 TWSE/TPEx 限流:空 body 或非 JSON 時退避重試。
    ⚠️TWSE 被限流時會回空 body(json() 直接噴 JSONDecodeError),重試後才拿得到資料。
    ⚠️TPEx OpenAPI 偶發 RemoteProtocolError,需帶 Connection: close(見 _REV_HEADERS)。"""
    last = ""
    for i in range(tries):
        if i:
            time.sleep(1.5 * i)  # 1.5s、3s 退避
        try:
            r = httpx.get(url, timeout=30, headers=headers or _MARGIN_UA)
            body = (r.text or "").strip()
            if body[:1] in ("{", "["):
                return r.json()
            last = f"HTTP {r.status_code} 空回應" if not body else f"非 JSON:{body[:60]}"
        except (httpx.HTTPError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        logger.warning("融資資料抓取第 %d 次失敗(%s):%s", i + 1, url[:50], last)
    raise MarginBusyError(last or "無回應")


def _twse_margin_bal(date: datetime.date):
    """上市:回 (前日餘額, 今日餘額) 融資金額仟元;查無資料回 None。"""
    url = ("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
           f"?date={date:%Y%m%d}&selectType=ALL&response=json")
    j = _margin_json(url)
    if j.get("stat") != "OK" or not j.get("tables"):
        # ⚠️TWSE 被限流時也回「很抱歉,沒有符合條件的資料」,與真的非交易日長得一樣
        # → 再等一下重查一次,兩次都沒有才當作真的沒資料
        time.sleep(2.5)
        j = _margin_json(url)
        if j.get("stat") != "OK" or not j.get("tables"):
            return None
    for r in j["tables"][0].get("data", []):  # 信用交易統計:找「融資金額(仟元)」列
        if r and "融資" in str(r[0]) and "金額" in str(r[0]):
            return _margin_num(r[4]), _margin_num(r[5])  # 前日餘額, 今日餘額
    return None


def _tpex_margin_bal(date: datetime.date):
    """上櫃:回 (前資餘額, 資餘額) 融資金仟元;查無資料回 None。"""
    url = ("https://www.tpex.org.tw/www/zh-tw/margin/balance"
           f"?date={date:%Y/%m/%d}&response=json")
    j = _margin_json(url)
    tables = j.get("tables") or []
    if not tables or not tables[0].get("totalCount"):  # 非交易日 totalCount=0
        return None
    for r in tables[0].get("summary", []):  # summary 裡的「融資金(仟元)」合計列
        if len(r) > 6 and str(r[1]).startswith("融資金"):
            return _margin_num(r[2]), _margin_num(r[6])  # 前資餘額, 資餘額
    return None


def fetch_margin_change(date=None, lookback: int = 10):
    """融資餘額增減(金額,仟元)。date=None → 從今天往回找最近有資料的交易日;
    指定 date 只查那天。回 dict(含兩市前/今餘額與增減、合計)或 None(找不到)。"""
    start = date or datetime.datetime.now(TW_TZ).date()
    span = 1 if date else lookback
    for i in range(span):
        d = start - datetime.timedelta(days=i)
        tw = _twse_margin_bal(d)
        tp = _tpex_margin_bal(d)
        if tw and tp:
            return {
                "date": d,
                "twse_prev": tw[0], "twse_today": tw[1], "twse_chg": tw[1] - tw[0],
                "tpex_prev": tp[0], "tpex_today": tp[1], "tpex_chg": tp[1] - tp[0],
                "total_chg": (tw[1] - tw[0]) + (tp[1] - tp[0]),
            }
    return None


def _parse_margin_date(arg: str):
    """使用者輸入 → date;空字串回 None(=最近交易日)。無法解析 raise ValueError。
    接受:7/28、07/28(當年)、2026/07/28、2026-07-28、20260728、115/07/28(民國)。"""
    s = arg.strip().replace("-", "/").replace(".", "/")
    if not s:
        return None
    if s.isdigit() and len(s) == 8:  # YYYYMMDD
        return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    parts = [p for p in s.split("/") if p]
    try:
        if len(parts) == 2:  # MM/DD → 當年
            today = datetime.datetime.now(TW_TZ).date()
            return datetime.date(today.year, int(parts[0]), int(parts[1]))
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 1911:  # 民國年
                y += 1911
            return datetime.date(y, m, d)
    except ValueError:
        pass
    raise ValueError(f"看不懂日期「{arg}」")


def _yi(thousand_ntd: int) -> float:
    """仟元 → 億元(1 億 = 100,000 仟元)。"""
    return thousand_ntd / 100000


def is_margin_command(text: str) -> bool:
    t = (text or "").strip()
    return t == "融資" or t.startswith("融資 ") or t.startswith("融資餘額")


def run_margin_query(text: str) -> str:
    """處理「融資 7/28」指令,回可 reply 的字串。
    只打「融資」或看不懂的參數 → 回空字串(不回覆,避免誤觸);「融資 用法」→ 用法說明。"""
    t = (text or "").strip()
    arg = ""
    for prefix in ("融資餘額", "融資"):
        if t.startswith(prefix):
            arg = t[len(prefix):].strip()
            break
    if not arg:
        return ""  # 只打「融資」→ 不回覆,避免一直誤觸
    if arg in ("用法", "help", "?", "？"):
        return ("📊 融資餘額查詢用法\n"
                "打「融資 日期」查該日兩市融資餘額增減\n"
                "日期:融資 7/28、融資 20260728、融資 2026-07-28、融資 115/07/28")
    try:
        date = _parse_margin_date(arg)
    except ValueError:
        return ""  # 看不懂日期 → 不回覆,避免誤觸
    try:
        info = fetch_margin_change(date)
    except MarginBusyError:
        logger.warning("融資資料來源忙碌/限流,已重試仍失敗")
        return "⏳ 證交所/櫃買資料暫時取不到(可能查詢過於頻繁),稍等一下再打一次"
    except Exception as e:  # noqa: BLE001
        logger.exception("融資餘額查詢失敗")
        return f"❌ 融資查詢失敗:{e}"
    if not info:
        return f"📊 {date:%m/%d} 查無融資餘額資料(可能非交易日),換一天試試"

    def _arrow(x):
        return "▲" if x > 0 else ("▼" if x < 0 else "－")

    def _row(name, chg, today):
        return f"{name} {_yi(chg):+,.2f} 億 {_arrow(chg)}  (餘額 {_yi(today):,.0f} 億)"

    d = info["date"]
    return (
        f"📊 融資餘額增減｜{d:%m/%d}\n"
        "─────────────\n"
        f"{_row('上市', info['twse_chg'], info['twse_today'])}\n"
        f"{_row('上櫃', info['tpex_chg'], info['tpex_today'])}\n"
        "─────────────\n"
        f"合計 {_yi(info['total_chg']):+,.2f} 億 {_arrow(info['total_chg'])}"
    )


# ==== 月營收公布日曆(每日快照差分,學各公司的公布日習慣)====
# 官方沒有「各公司公告日」欄位(出表日期兩市全部同一值=報表產生日),
# 只能每天抓一次全市場彙總,記下每檔「第一次出現該月份資料」的日期 = 實際公告日。
_REV_TWSE = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
_REV_TPEX = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
_REV_HEADERS = {**_MARGIN_UA, "Connection": "close"}  # TPEx 不加會偶發 RemoteProtocolError
_REV_MIN_SAMPLES = 3      # 少於這期數不做預估
_REV_FLIP_RATIO = 0.6     # 同一天首見佔比超過此值 → 疑似整月批次翻轉,該月不可用


def fetch_revenue_all() -> dict:
    """抓上市+上櫃月營收彙總,回 {代碼: {'ym','name','rev','yoy'}}。ym=資料年月(民國,如 11507)。"""
    out: dict[str, dict] = {}
    for url in (_REV_TWSE, _REV_TPEX):
        rows = _margin_json(url, headers=_REV_HEADERS)
        for r in rows or []:
            code = str(r.get("公司代號") or "").strip()
            ym = str(r.get("資料年月") or "").strip()
            if not code or not ym:
                continue
            out[code] = {
                "ym": ym,
                "name": str(r.get("公司名稱") or "").strip(),
                "rev": str(r.get("營業收入-當月營收") or "").strip(),
                "yoy": str(r.get("營業收入-去年同月增減(%)") or "").strip(),
            }
    return out


def _roc_month(d: datetime.date) -> str:
    """該日對應「應公布的資料年月」= 上個月,民國格式如 11507。"""
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f"{y - 1911}{m:02d}"


def _rev_confidence(entries: dict, day: int) -> str:
    """依分布判斷該月資料可不可信。entries={code:{'day','censored'}}。"""
    if not entries:
        return "累積中"
    days = collections.Counter(v["day"] for v in entries.values())
    if days.most_common(1)[0][1] / len(entries) > _REV_FLIP_RATIO:
        # 絕大多數同一天首見 → 來源可能是整月批次翻轉,而非逐日更新
        return "低-疑似批次翻轉"
    if sum(1 for v in entries.values() if v["censored"]) / len(entries) > 0.5:
        return "低-首月censored"
    return "可用" if day >= 15 else "累積中"


def sync_revenue_calendar(target_month: str = "", today: datetime.date | None = None) -> dict:
    """每日任務:抓兩市營收,把「第一次出現目標月份資料」的公司記進 Notion。
    冪等:同一天重跑不會重複寫入(已記錄的代碼直接跳過)。"""
    today = today or datetime.datetime.now(TW_TZ).date()
    if not target_month and today.day > 20:
        logger.info("營收日曆:%s 非公布期(>20 日),略過", today)
        return {"skipped": True, "month": "", "new": 0, "total": 0}
    month = target_month or _roc_month(today)

    all_rev = fetch_revenue_all()
    rec = nt.read_revenue_month(month)
    if rec is None:
        page_id = nt.create_revenue_month(month, today.isoformat())
        existing, last_sync = {}, ""
    else:
        page_id, existing, last_sync = rec["page_id"], rec["entries"], rec["last_update"]

    # censored:首次快照(1 號以後才開始記)或中間斷過 → 只知「不晚於今天」,不是精確公告日
    censored = True
    if last_sync:
        try:
            censored = (today - datetime.date.fromisoformat(last_sync[:10])).days > 1
        except ValueError:
            censored = True
    elif rec is None and today.day == 1:
        censored = False  # 1 號就開始記,沒有漏掉的區間

    new_items, new_map = [], {}
    for code, info in all_rev.items():
        if info["ym"] != month or code in existing:
            continue
        new_items.append(nt._rev_entry_str(code, today.day, censored))
        new_map[code] = {"day": today.day, "censored": censored}

    nt.append_revenue_entries(page_id, sorted(new_items))
    combined = {**existing, **new_map}
    nt.update_revenue_month_props(
        page_id, len(combined), _rev_confidence(combined, today.day), today.isoformat()
    )
    logger.info("營收日曆 %s:新增 %d 家,累計 %d 家%s",
                month, len(new_items), len(combined), "(censored)" if censored else "")
    return {"skipped": False, "month": month, "new": len(new_items),
            "total": len(combined), "censored": censored}


# ---- 依歷史推估各檔公布日 ----
_rev_hist: dict = {"at": None, "data": {}}
_REV_HIST_TTL = 6 * 3600  # 一天只變一次,快取 6 小時(讀全部月份頁要數十次 API,不能每次查詢都重讀)


def load_revenue_history(force: bool = False) -> dict:
    """讀所有可用月份,回 {代碼: [公布日, ...]}。排除低可信度月份與 censored 記錄。"""
    now = time.time()
    if not force and _rev_hist["at"] and now - _rev_hist["at"] < _REV_HIST_TTL:
        return _rev_hist["data"]
    hist: dict[str, list[int]] = {}
    for m in nt.list_revenue_months():
        if m["confidence"].startswith("低"):
            continue  # 該月資料本身不可信(首月censored / 疑似批次翻轉)
        rec = nt.read_revenue_month(m["month"])
        if not rec:
            continue
        for code, info in rec["entries"].items():
            if info["censored"]:
                continue  # 只知「不晚於該日」,不是精確公告日
            hist.setdefault(code, []).append(info["day"])
    _rev_hist.update(at=now, data=hist)
    return hist


def predict_revenue_day(code: str, hist: dict | None = None) -> dict | None:
    """回 {'day','iqr','confidence','samples'};樣本不足回 {'samples':n,'day':None}。"""
    hist = hist if hist is not None else load_revenue_history()
    days = sorted(hist.get(str(code).strip(), []))
    if len(days) < _REV_MIN_SAMPLES:
        return {"day": None, "samples": len(days), "iqr": None, "confidence": ""}
    day = round(statistics.median(days))
    if len(days) >= 4:
        q1, _, q3 = statistics.quantiles(days, n=4, method="inclusive")
        iqr = q3 - q1
    else:
        iqr = days[-1] - days[0]
    conf = "高" if iqr <= 1 else ("中" if iqr <= 3 else "低")
    return {"day": day, "samples": len(days), "iqr": round(iqr, 1), "confidence": conf}


def is_revenue_command(text: str) -> bool:
    t = (text or "").strip()
    return t == "營收" or t.startswith("營收 ") or t.startswith("營收日曆")


def _rev_usage() -> str:
    return ("📅 營收公布日曆用法\n"
            "・營收日曆 → 未來 7 天預估要公布的追蹤股\n"
            "・營收日曆 8/5 → 指定日預估名單\n"
            "・營收 2330 → 單檔預估公布日與歷史\n"
            "(依每日快照學各公司習慣,需累積數期才有預估)")


def run_revenue_query(text: str) -> str:
    """「營收日曆 [日期]」/「營收 代碼」。無參數或看不懂 → 回空字串(不回覆,避免誤觸)。"""
    t = (text or "").strip()
    body = t[len("營收日曆"):].strip() if t.startswith("營收日曆") else t[len("營收"):].strip()
    calendar_mode = t.startswith("營收日曆")
    if not calendar_mode and not body:
        return ""
    if body in ("用法", "help", "?", "？"):
        return _rev_usage()

    try:
        hist = load_revenue_history()
    except Exception as e:  # noqa: BLE001
        logger.exception("營收歷史讀取失敗")
        return f"❌ 營收查詢失敗:{e}"

    if calendar_mode:
        today = datetime.datetime.now(TW_TZ).date()
        if body:
            try:
                target = _parse_margin_date(body)
            except ValueError:
                return ""
            days = [target] if target else [today]
        else:
            days = [today + datetime.timedelta(days=i) for i in range(7)]
        stocks = [s for s in nt.list_stocks() if s["code"]]
        lines, hit = [f"📅 營收公布預估｜{'指定日' if body else '未來 7 天'}", "─" * 13], 0
        for d in days:
            names = []
            for s in stocks:
                p = predict_revenue_day(s["code"], hist)
                if p and p["day"] == d.day:
                    names.append(f"  {s['label']}(可信度{p['confidence']})")
            if names:
                hit += len(names)
                lines.append(f"{d.month}/{d.day:02d}")
                lines.extend(names)
        if not hit:
            samples = sum(1 for s in stocks if hist.get(s["code"]))
            lines.append(f"— 無預估 —\n目前累積 {len(nt.list_revenue_months())} 期、"
                         f"{samples} 檔追蹤股有記錄。\n需至少 {_REV_MIN_SAMPLES} 期才會出現預估。")
        else:
            lines.append("─" * 13)
            lines.append("* 依歷史公布日推估,非官方公告")
        return "\n".join(lines)

    stock = nt.find_stock_page(body, body)
    if not stock and not re.fullmatch(r"\d{3,6}[A-Za-z]?", body):
        return ""  # 既不是主表裡的名稱、也不像股票代號 → 不回覆,避免誤觸
    label = stock["label"] if stock else body
    p = predict_revenue_day(body, hist)
    days = sorted(hist.get(body, []), reverse=True)
    if not p or p["day"] is None:
        n = p["samples"] if p else 0
        return (f"📅 {label} 營收公布預估\n{'─' * 13}\n"
                f"資料不足(目前 {n} 期,需 {_REV_MIN_SAMPLES} 期)\n"
                f"每月自動累積中,累積後即可預估")
    return (f"📅 {label} 營收公布預估\n{'─' * 13}\n"
            f"預估公布日:每月 {p['day']} 日\n"
            f"可信度:{p['confidence']}(離散度 {p['iqr']} 天)\n"
            f"樣本:{p['samples']} 期  歷史:{', '.join(f'{d}日' for d in days[:8])}\n"
            f"{'─' * 13}\n* 依歷史推估,非官方公告")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    res = sync_earnings_calls(dry_run=dry)
    print(f"\n{'[DRY RUN] ' if dry else ''}比對到追蹤股 {res['matched']} 筆,"
          f"{'將新增' if dry else '已新增'} {res['added']},略過(已存在){res['skipped']}")
    for it in res["added_items"]:
        print(f"  + {it}")

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
import logging
import datetime

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
        _push_line("\n".join(lines))
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


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    res = sync_earnings_calls(dry_run=dry)
    print(f"\n{'[DRY RUN] ' if dry else ''}比對到追蹤股 {res['matched']} 筆,"
          f"{'將新增' if dry else '已新增'} {res['added']},略過(已存在){res['skipped']}")
    for it in res["added_items"]:
        print(f"  + {it}")

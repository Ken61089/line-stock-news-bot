"""
公開資訊自動抓取 → 寫進 Notion 時程庫。

目前來源:富聯網(money-link)國內法說會時間表(靜態頁,big5)。
只抓「Notion 個股主表裡追蹤的股票」、未來日期、含所有出席(自辦法說會 +
出席券商論壇/座談會),寫成時程庫「法說會」事件,並依 (個股, 日期) 去重。

由 calendar_notify 的排程每日觸發;也可用 LINE 指令「更新法說會」手動觸發。
"""

import re
import logging
import datetime

import httpx

import notion_timeline as nt

logger = logging.getLogger("line-news-bot.fetchers")

MONEY_LINK_URL = "https://www.money-link.com.tw/stxba/imwcontent0.asp?page=INVC1&ID=INVC1"
TW_TZ = nt.TW_TZ

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


_EARNINGS_TYPE = "法說會"
_HORIZON_DAYS = 120  # 只抓未來 120 天內的法說會


def fetch_earnings_rows() -> list[dict]:
    """抓 money-link 法說會表,回傳 [{date(YYYY-MM-DD), code, name, time, place, message}]。"""
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
        })
    return rows


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
            )
            existing.add((stock["id"], r["date"]))  # 同批內也去重
            added += 1
        except nt.NotionError as e:
            logger.warning("寫入法說會失敗(%s %s):%s", r["code"], r["date"], e)

    logger.info("法說會同步:比對到 %d 筆追蹤股,新增 %d,略過(已存在)%d", matched, added, skipped)
    return {"matched": matched, "added": added, "skipped": skipped, "added_items": added_items}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry = "--dry" in sys.argv or "--dry-run" in sys.argv
    res = sync_earnings_calls(dry_run=dry)
    print(f"\n{'[DRY RUN] ' if dry else ''}比對到追蹤股 {res['matched']} 筆,"
          f"{'將新增' if dry else '已新增'} {res['added']},略過(已存在){res['skipped']}")
    for it in res["added_items"]:
        print(f"  + {it}")

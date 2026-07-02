"""
Notion 時間軸寫入模組。

把「時程事件」寫進 Notion「3. 新聞與時程動態庫」,並依代號關聯到「個股主表」,
Notion 端的 rollup(💡 自動辨別:相關族群)會自動帶出概念族群,時間軸即可依概念分群。

需要的環境變數:
  NOTION_TOKEN         Notion internal integration 的密鑰(secret_...)
  NOTION_TIMELINE_DS   時程庫 data source id(預設已填正式值)
  NOTION_STOCK_DS      個股主表 data source id(預設已填正式值)
  NOTION_VERSION       Notion API 版本(預設 2025-09-03,支援 data source)
"""

import os
import re
import datetime

import httpx

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2025-09-03")
TIMELINE_DS = os.environ.get("NOTION_TIMELINE_DS", "388da86b-7f8b-8139-ad5a-000b9477f4ef").strip()
STOCK_DS = os.environ.get("NOTION_STOCK_DS", "388da86b-7f8b-817c-93ca-000b5553ac22").strip()

_API = "https://api.notion.com/v1"
_STOCK_CODE_RE = re.compile(r"\d{3,6}")
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 時程庫「事件類型」的合法選項(需與 Notion 端一致;AI 抽出的類型會對映到這裡)
EVENT_TYPES = {
    "擴廠進度", "試產進度", "量產進度", "發行可轉債", "CB掛牌", "CB拆解",
    "法說會", "股東會", "除權息", "增減資", "營收公布", "財報公布",
    "財報利空/多", "展覽/政策", "盤後隨筆", "每日關注", "其他",
}


class NotionError(Exception):
    pass


def enabled() -> bool:
    return bool(NOTION_TOKEN)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _post(path: str, payload: dict) -> dict:
    r = httpx.post(f"{_API}{path}", headers=_headers(), json=payload, timeout=20)
    if r.status_code >= 300:
        raise NotionError(f"Notion API {r.status_code}: {r.text[:300]}")
    return r.json()


def _patch(path: str, payload: dict) -> dict:
    r = httpx.patch(f"{_API}{path}", headers=_headers(), json=payload, timeout=20)
    if r.status_code >= 300:
        raise NotionError(f"Notion API {r.status_code}: {r.text[:300]}")
    return r.json()


def archive_events_by_type_date(event_type: str, date_iso: str) -> int:
    """封存(移除)指定「事件類型」且「關鍵日期」等於 date_iso 的所有事件,回傳筆數。
    用於「每日關注」重打時,先清掉同一天的舊筆記再寫新的(採最新版)。"""
    payload = {
        "filter": {
            "and": [
                {"property": "事件類型", "select": {"equals": event_type}},
                {"property": "關鍵日期", "date": {"equals": date_iso}},
            ]
        },
        "page_size": 100,
    }
    data = _post(f"/data_sources/{TIMELINE_DS}/query", payload)
    n = 0
    for pg in data.get("results", []):
        _patch(f"/pages/{pg['id']}", {"archived": True})
        n += 1
    return n


def find_stock_page(code: str, name: str = "") -> dict | None:
    """在個股主表用代號(優先)或名稱找個股頁。
    回傳 {'id', 'label', 'concept_ids'} 或 None;concept_ids 為該股「🔗 隸屬概念」的概念頁 ids。"""
    queries = []
    code = (code or "").strip()
    name = (name or "").strip()
    m = _STOCK_CODE_RE.search(code) or _STOCK_CODE_RE.search(name)
    if m:
        queries.append(m.group(0))
    if name:
        queries.append(name)
    for q in queries:
        data = _post(
            f"/data_sources/{STOCK_DS}/query",
            {"filter": {"property": "Name", "title": {"contains": q}}, "page_size": 5},
        )
        results = data.get("results", [])
        if results:
            page = results[0]
            props = page.get("properties", {})
            title = props.get("Name", {}).get("title", [])
            label = "".join(t.get("plain_text", "") for t in title).strip()
            concept_ids = [
                r["id"] for r in props.get("🔗 隸屬概念", {}).get("relation", []) if r.get("id")
            ]
            return {"id": page["id"], "label": label, "concept_ids": concept_ids}
    return None


def create_stock_page(code: str, name: str) -> dict:
    """在個股主表新增一檔(只填 Name,格式「代號 名稱」)。回傳 {'id','label','concept_ids':[]}。"""
    code = (code or "").strip()
    name = (name or "").strip()
    label = f"{code} {name}".strip() or code or name or "(未命名)"
    data = _post(
        "/pages",
        {
            "parent": {"type": "data_source_id", "data_source_id": STOCK_DS},
            "properties": {"Name": {"title": [{"text": {"content": label[:200]}}]}},
        },
    )
    return {"id": data.get("id", ""), "label": label, "concept_ids": []}


def list_stocks() -> list[dict]:
    """列出個股主表所有個股,回傳 [{'id','label','code','concept_ids'}](自動翻頁)。"""
    results: list[dict] = []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = _post(f"/data_sources/{STOCK_DS}/query", payload)
        for page in data.get("results", []):
            props = page.get("properties", {})
            title = props.get("Name", {}).get("title", [])
            label = "".join(t.get("plain_text", "") for t in title).strip()
            m = _STOCK_CODE_RE.search(label)
            concept_ids = [
                r["id"] for r in props.get("🔗 隸屬概念", {}).get("relation", []) if r.get("id")
            ]
            results.append({
                "id": page["id"],
                "label": label,
                "code": m.group(0) if m else "",
                "concept_ids": concept_ids,
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def add_event(
    title: str,
    date_start: str,
    date_end: str = "",
    event_type: str = "其他",
    stock_page_id: str = "",
    concept_ids: list | None = None,
    note: str = "",
    source: str = "LINE",
    status: str = "",
    link: str = "",
) -> dict:
    """建立一筆時程事件頁,回傳 {'url'}。link 會寫進「來源連結」。"""
    if event_type not in EVENT_TYPES:
        event_type = "其他"
    if not status:
        status = _infer_status(date_start, date_end)

    props: dict = {
        "Name": {"title": [{"text": {"content": title[:200] or "(未命名事件)"}}]},
        "事件類型": {"select": {"name": event_type}},
        "資料來源": {"select": {"name": source}},
        "狀態": {"select": {"name": status}},
    }
    if date_start:
        date_val = {"start": date_start}
        if date_end:
            date_val["end"] = date_end
        props["關鍵日期"] = {"date": date_val}
    if note:
        props["Quick Note"] = {"rich_text": [{"text": {"content": note[:1900]}}]}
    if stock_page_id:
        props["🔗 關聯個股"] = {"relation": [{"id": stock_page_id}]}
    if concept_ids:
        props["🔗 概念族群"] = {"relation": [{"id": cid} for cid in concept_ids]}
    if link:
        props["來源連結"] = {"url": link}

    data = _post(
        "/pages",
        {"parent": {"type": "data_source_id", "data_source_id": TIMELINE_DS}, "properties": props},
    )
    return {"url": data.get("url", "")}


def _infer_status(date_start: str, date_end: str = "") -> str:
    """日期在未來 → 預定;否則 → 已發生。無法解析 → 預定。"""
    today = datetime.datetime.now(TW_TZ).date()
    ref = date_end or date_start
    try:
        d = datetime.date.fromisoformat(ref[:10])
    except (ValueError, TypeError):
        return "預定"
    return "已發生" if d < today else "預定"

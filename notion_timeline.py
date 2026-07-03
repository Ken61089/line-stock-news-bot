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
CONCEPT_DS = os.environ.get("NOTION_CONCEPT_DS", "388da86b-7f8b-8177-8d81-000badfe5200").strip()
GLOBAL_DS = os.environ.get("NOTION_GLOBAL_DS", "36694cc2-76ec-469e-9e78-1d0df333d79a").strip()
KNOWLEDGE_DS = os.environ.get("NOTION_KNOWLEDGE_DS", "517dd7f8-6804-4048-bd0c-529a8cbe8b1a").strip()

_API = "https://api.notion.com/v1"
_STOCK_CODE_RE = re.compile(r"\d{3,6}")
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 時程庫「事件類型」的合法選項(需與 Notion 端一致;AI 抽出的類型會對映到這裡)
EVENT_TYPES = {
    "擴廠進度", "試產進度", "量產進度", "發行可轉債", "CB掛牌", "CB拆解",
    "法說會", "股東會", "除權息", "增減資", "營收公布", "財報公布",
    "財報利空/多", "展覽/政策", "盤後隨筆", "每日關注", "隨手筆記", "經濟數據", "其他",
    # 新聞機器人搬進 Notion 後用的類型(時間軸視圖可用這幾個把「新聞卡」篩掉、只看真實時程)
    "個股新聞", "產業新聞", "個股產業報告",
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


def _get(path: str) -> dict:
    r = httpx.get(f"{_API}{path}", headers=_headers(), timeout=20)
    if r.status_code >= 300:
        raise NotionError(f"Notion API {r.status_code}: {r.text[:300]}")
    return r.json()


def _dedup(items) -> list:
    """保序去重、去空白空值。"""
    out, seen = [], set()
    for x in items or []:
        x = (x or "").strip() if isinstance(x, str) else x
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _multi_select(names) -> list:
    """multi_select 選項名稱不可含逗號;清乾淨並保序去重。"""
    out, seen = [], set()
    for n in names or []:
        n = (n or "").replace(",", " ").replace("，", " ").strip()[:100]
        if n and n not in seen:
            seen.add(n)
            out.append({"name": n})
    return out


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


def list_notes_by_date(date_iso: str, event_type: str = "隨手筆記") -> list[dict]:
    """查某日某類型事件,依「記錄精確時間」排序,回傳 [{'title','time'(台灣 HH:MM)}]。
    Notion 所有時間欄位(created_time、日期)都只有分鐘精度,無法秒級排序;隨手筆記把
    記錄當下的完整 ISO 時間寫在 Quick Note 首行當排序鍵,故此處在 Python 端秒級排序。"""
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
    out = []
    for pg in data.get("results", []):
        props = pg.get("properties", {})
        title = "".join(
            t.get("plain_text", "") for t in props.get("Name", {}).get("title", [])
        ).strip()
        quick = "".join(t.get("plain_text", "") for t in props.get("Quick Note", {}).get("rich_text", []))
        stamp = quick.splitlines()[0].strip() if quick else ""
        hhmm = ""
        try:
            hhmm = datetime.datetime.fromisoformat(stamp).astimezone(TW_TZ).strftime("%H:%M")
        except (ValueError, TypeError):
            stamp = ""  # 排序鍵無效者(空字串)排最前
        out.append({"title": title or "(空白筆記)", "time": hhmm, "_k": stamp})
    out.sort(key=lambda x: x["_k"])
    return [{"title": x["title"], "time": x["time"]} for x in out]


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
    stock_page_ids: list | None = None,
) -> dict:
    """建立一筆時程事件頁,回傳 {'url'}。link 會寫進「來源連結」。
    stock_page_id(單檔,向下相容)與 stock_page_ids(多檔)可並用,內部合併去重。"""
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
    stock_ids = _dedup([*(stock_page_ids or []), stock_page_id])
    if stock_ids:
        props["🔗 關聯個股"] = {"relation": [{"id": i} for i in stock_ids]}
    if concept_ids:
        props["🔗 概念族群"] = {"relation": [{"id": cid} for cid in _dedup(concept_ids)]}
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


# ==========================================================
# 概念族群:找/建概念頁、把概念掛到個股(取代舊 Google Sheet 概念股主表)
# ==========================================================
_concept_cache: dict = {}
_concept_names_cache: list | None = None  # 概念名稱白名單快取(新建概念時失效)


def find_or_create_concept(name: str) -> str:
    """在概念族群庫用名稱找概念頁,沒有就建立。回傳頁 id(找不到/建不出回 "")。"""
    global _concept_names_cache
    name = (name or "").strip()
    if not name:
        return ""
    if name in _concept_cache:
        return _concept_cache[name]
    data = _post(
        f"/data_sources/{CONCEPT_DS}/query",
        {"filter": {"property": "Name", "title": {"equals": name}}, "page_size": 1},
    )
    results = data.get("results", [])
    if results:
        cid = results[0]["id"]
    else:
        created = _post(
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": CONCEPT_DS},
                "properties": {"Name": {"title": [{"text": {"content": name[:200]}}]}},
            },
        )
        cid = created.get("id", "")
        _concept_names_cache = None  # 新建了概念 → 白名單失效,下一則會重抓
    if cid:
        _concept_cache[name] = cid
    return cid


def concept_names() -> list:
    """回傳概念族群庫現有的概念名稱清單(快取;新建概念時自動失效)。
    給新聞 AI 標概念時當白名單,使名稱一致;隨新增資料自動成長。"""
    global _concept_names_cache
    if _concept_names_cache is None:
        _concept_names_cache = [c["name"] for c in list_concepts() if c.get("name")]
    return list(_concept_names_cache)


def ensure_concepts(names: list) -> list:
    """把一串概念名稱轉成概念頁 id(保序去重、逐一找或建)。"""
    ids = []
    for n in _dedup(names):
        cid = find_or_create_concept(n)
        if cid:
            ids.append(cid)
    return _dedup(ids)


def link_stock_concepts(stock_page_id: str, concept_ids: list) -> bool:
    """把 concept_ids 併進個股的「🔗 隸屬概念」(讀取現有→union→有變才寫)。
    dual relation 會自動補概念庫的成員個股。回傳是否有更新。"""
    if not stock_page_id or not concept_ids:
        return False
    page = _get(f"/pages/{stock_page_id}")
    existing = [
        r["id"] for r in page.get("properties", {}).get("🔗 隸屬概念", {}).get("relation", []) if r.get("id")
    ]
    merged = _dedup([*existing, *concept_ids])
    if len(merged) == len(existing):
        return False
    _patch(
        f"/pages/{stock_page_id}",
        {"properties": {"🔗 隸屬概念": {"relation": [{"id": i} for i in merged]}}},
    )
    return True


def find_news_by_link(ds_id: str, link: str):
    """以「來源連結」url 精確比對查重;回傳既有頁或 None(link 空一律 None)。"""
    link = (link or "").strip()
    if not link:
        return None
    data = _post(
        f"/data_sources/{ds_id}/query",
        {"filter": {"property": "來源連結", "url": {"equals": link}}, "page_size": 1},
    )
    results = data.get("results", [])
    return results[0] if results else None


# ==========================================================
# 全球局勢動態庫 / 知識補充庫 寫入
# ==========================================================
def add_global(title, summary="", topics=None, markets=None, date_start="",
               note="", link="", source="新聞bot", status="") -> dict:
    if not status:
        status = _infer_status(date_start) if date_start else "已發生"
    props: dict = {
        "Name": {"title": [{"text": {"content": (title or "(未命名)")[:200]}}]},
        "資料來源": {"select": {"name": source}},
        "狀態": {"select": {"name": status}},
    }
    if summary:
        props["AI 摘要"] = {"rich_text": [{"text": {"content": summary[:1900]}}]}
    if topics:
        props["影響主題"] = {"multi_select": _multi_select(topics)}
    if markets:
        props["受影響市場資產"] = {"multi_select": _multi_select(markets)}
    if date_start:
        props["關鍵日期"] = {"date": {"start": date_start}}
    if note:
        props["Quick Note"] = {"rich_text": [{"text": {"content": note[:1900]}}]}
    if link:
        props["來源連結"] = {"url": link}
    data = _post("/pages", {"parent": {"type": "data_source_id", "data_source_id": GLOBAL_DS}, "properties": props})
    return {"url": data.get("url", "")}


def add_knowledge(topic, key_points_text="", keywords=None, link="", source="新聞bot") -> dict:
    props: dict = {
        "Name": {"title": [{"text": {"content": (topic or "(未命名)")[:200]}}]},
        "資料來源": {"select": {"name": source}},
    }
    if key_points_text:
        props["重點整理"] = {"rich_text": [{"text": {"content": key_points_text[:1900]}}]}
    if keywords:
        props["關鍵字"] = {"multi_select": _multi_select(keywords)}
    if link:
        props["來源連結"] = {"url": link}
    data = _post("/pages", {"parent": {"type": "data_source_id", "data_source_id": KNOWLEDGE_DS}, "properties": props})
    return {"url": data.get("url", "")}


# ==========================================================
# 反向查詢用:讀近期頁、把頁面屬性攤平成文字
# ==========================================================
def list_concepts() -> list:
    """列出概念族群庫所有概念,回傳 [{'id','name'}](自動翻頁)。"""
    results, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = _post(f"/data_sources/{CONCEPT_DS}/query", payload)
        for page in data.get("results", []):
            title = page.get("properties", {}).get("Name", {}).get("title", [])
            name = "".join(t.get("plain_text", "") for t in title).strip()
            results.append({"id": page["id"], "name": name})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def recent_pages(ds_id: str, n: int = 80) -> list:
    """取某資料庫近期頁(依建立時間新到舊)。"""
    data = _post(
        f"/data_sources/{ds_id}/query",
        {"sorts": [{"timestamp": "created_time", "direction": "descending"}], "page_size": min(max(n, 1), 100)},
    )
    return data.get("results", [])


def _prop_to_text(prop: dict, id_name_map: dict | None = None) -> str:
    t = prop.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", [])).strip()
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])).strip()
    if t == "select":
        s = prop.get("select")
        return s.get("name", "") if s else ""
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if t == "date":
        d = prop.get("date")
        if not d:
            return ""
        return d.get("start", "") + (f"~{d['end']}" if d.get("end") else "")
    if t == "url":
        return prop.get("url", "") or ""
    if t == "relation":
        ids = [r.get("id") for r in prop.get("relation", []) if r.get("id")]
        if id_name_map:
            return ", ".join(id_name_map.get(i, "") or i for i in ids)
        return ", ".join(ids)
    return ""


def page_to_text(page: dict, id_name_map: dict | None = None, skip=("💡 自動辨別：相關族群",)) -> str:
    """把一頁的所有屬性攤平成「欄名:值 | 欄名:值」,relation 用 id_name_map 換成名稱。"""
    parts = []
    for name, prop in page.get("properties", {}).items():
        if name in skip:
            continue
        val = _prop_to_text(prop, id_name_map)
        if val:
            parts.append(f"{name}:{val}")
    return " | ".join(parts)


# ==========================================================
# 修正指令用:概念/個股關聯的讀寫、頁面封存
# ==========================================================
def archive_page(page_id: str) -> None:
    _patch(f"/pages/{page_id}", {"archived": True})


def set_page_props(page_id: str, props: dict) -> None:
    _patch(f"/pages/{page_id}", {"properties": props})


def find_concept(name: str):
    """精確找概念頁(不建立)。回傳 {'id','name'} 或 None。"""
    name = (name or "").strip()
    if not name:
        return None
    data = _post(
        f"/data_sources/{CONCEPT_DS}/query",
        {"filter": {"property": "Name", "title": {"equals": name}}, "page_size": 1},
    )
    r = data.get("results", [])
    if not r:
        return None
    title = r[0].get("properties", {}).get("Name", {}).get("title", [])
    return {"id": r[0]["id"], "name": "".join(t.get("plain_text", "") for t in title).strip()}


def rename_concept(concept_id: str, new_name: str) -> None:
    global _concept_names_cache
    _patch(f"/pages/{concept_id}", {"properties": {"Name": {"title": [{"text": {"content": new_name[:200]}}]}}})
    _concept_cache.clear()  # 名稱→id 快取可能過期
    _concept_names_cache = None


def _relation_ids(page: dict, prop_name: str) -> list:
    return [r["id"] for r in page.get("properties", {}).get(prop_name, {}).get("relation", []) if r.get("id")]


def concept_member_ids(concept_id: str) -> list:
    return _relation_ids(_get(f"/pages/{concept_id}"), "🔗 成員個股")


def get_stock_concepts(stock_id: str) -> list:
    return _relation_ids(_get(f"/pages/{stock_id}"), "🔗 隸屬概念")


def set_stock_concepts(stock_id: str, ids: list) -> None:
    _patch(f"/pages/{stock_id}", {"properties": {"🔗 隸屬概念": {"relation": [{"id": i} for i in _dedup(ids)]}}})


def unlink_stock_concept(stock_id: str, concept_id: str) -> bool:
    cur = get_stock_concepts(stock_id)
    kept = [i for i in cur if i != concept_id]
    if len(kept) == len(cur):
        return False
    set_stock_concepts(stock_id, kept)
    return True


def stock_news_ids(stock_id: str) -> list:
    return _relation_ids(_get(f"/pages/{stock_id}"), "🔗 相關新聞時程")


def replace_event_stock(event_id: str, old_id: str, new_id: str) -> None:
    """把某事件的「關聯個股」裡的 old_id 換成 new_id(去重)。"""
    ids = _relation_ids(_get(f"/pages/{event_id}"), "🔗 關聯個股")
    ids2 = _dedup([new_id if i == old_id else i for i in ids])
    _patch(f"/pages/{event_id}", {"properties": {"🔗 關聯個股": {"relation": [{"id": i} for i in ids2]}}})

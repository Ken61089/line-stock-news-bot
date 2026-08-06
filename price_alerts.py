"""盤中事件提醒:條件到了就把你預先寫好的關注內容推到 LINE。

用途舉例:「大盤當日跌 1000 點 → 提醒我打 BBU、記憶體、3324」。
重點是**觸發時把當初的想法原封不動丟回你眼前**,不必當下再想一次該看什麼。

資料源:TWSE MIS 即時報價 `mis.twse.com.tw/stock/api/getStockInfo.jsp`
(免費、無 Cloudflare、一次可帶多檔;需帶 Referer)。回傳欄位:
  z=成交價 y=昨收 o=開盤 h=最高 l=最低 d=交易日期 t=時間 ex=tse/otc
⚠️ `d` 是**該筆報價的交易日**,不等於今天 —— 非交易日打 API 會回上一個交易日的資料,
   不比對 `d` 的話週末每 15 分鐘都會拿昨天的跌幅重複觸發。

設定入口是 LINE 指令「提醒 …」,正本存 Notion「5. 事件提醒」。
"""

import os
import re
import logging
import datetime

import httpx

import notion_timeline as nt

logger = logging.getLogger(__name__)

ALERT_DS = os.environ.get("NOTION_ALERT_DS", "aec23e1e-4782-4346-b997-9283f5ae7138").strip()
_QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",  # 沒帶會被擋
}
TW_TZ = nt.TW_TZ

# 一天最多推幾則提醒,防止盤勢劇烈時把 LINE 免費額度(200 則/月)吃光
DAILY_PUSH_CAP = int(os.environ.get("ALERT_DAILY_CAP", "5"))
_pushed_today: dict = {"date": "", "n": 0}

INDEX_CODE = "t00"  # 發行量加權股價指數
_INDEX_WORDS = {"大盤", "加權", "台股", "指數", "t00", "TAIEX"}

CONDITIONS = ("當日跌N點", "當日漲N點", "跌破", "漲破")


# ---------------------------------------------------------------- 報價

def fetch_quotes(targets: list[tuple]) -> dict:
    """targets = [(code, market)],market 是 'tse'/'otc'。回 {code: {...}}。
    一次請求可帶多檔,所以整批查只花一個 request。"""
    if not targets:
        return {}
    ex_ch = "|".join(f"{mk or 'tse'}_{code}.tw" for code, mk in targets)
    r = httpx.get(_QUOTE_URL, params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                  headers=_QUOTE_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    out = {}
    for m in data.get("msgArray", []):
        code = (m.get("c") or "").strip()
        if not code:
            continue
        out[code] = {
            "name": (m.get("n") or "").strip(),
            "price": _f(m.get("z")) or _f(m.get("o")),  # 尚未成交時退回開盤價
            "prev_close": _f(m.get("y")),
            "high": _f(m.get("h")),
            "low": _f(m.get("l")),
            "date": (m.get("d") or "").strip(),
            "time": (m.get("t") or "").strip(),
            "ex": (m.get("ex") or "").strip(),
        }
    return out


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def resolve_market(code: str) -> str:
    """查這檔在上市還是上櫃(tse/otc)。新增提醒時查一次就存起來,之後不必再查。"""
    if code == INDEX_CODE:
        return "tse"
    try:
        q = fetch_quotes([(code, "tse"), (code, "otc")])
    except httpx.HTTPError:
        return "tse"
    hit = q.get(code)
    return (hit or {}).get("ex") or "tse"


# ---------------------------------------------------------------- Notion 讀寫

def _alert_to_dict(page: dict) -> dict:
    p = page.get("properties", {})

    def txt(k):
        return "".join(t.get("plain_text", "") for t in p.get(k, {}).get("rich_text", [])).strip()

    return {
        "id": page["id"],
        "title": "".join(t.get("plain_text", "") for t in p.get("Name", {}).get("title", [])).strip(),
        "code": txt("標的"),
        "market": (p.get("市場", {}).get("select") or {}).get("name", "tse"),
        "cond": (p.get("條件", {}).get("select") or {}).get("name", ""),
        "threshold": p.get("閾值", {}).get("number"),
        "content": txt("提醒內容"),
        "enabled": bool(p.get("啟用", {}).get("checkbox")),
        "repeat": (p.get("重複方式", {}).get("select") or {}).get("name", "每日一次"),
        "last_fired": ((p.get("最後觸發日", {}).get("date") or {}).get("start") or "")[:10],
        "fired": p.get("觸發次數", {}).get("number") or 0,
    }


def list_alerts(only_enabled: bool = False) -> list[dict]:
    payload = {"page_size": 100}
    if only_enabled:
        payload["filter"] = {"property": "啟用", "checkbox": {"equals": True}}
    data = nt._post(f"/data_sources/{ALERT_DS}/query", payload)
    return [_alert_to_dict(pg) for pg in data.get("results", [])]


def add_alert(code: str, market: str, cond: str, threshold: float, content: str,
              stock_page_id: str = "", repeat: str = "只觸發一次") -> dict:
    """預設**只觸發一次**:推播後自動停用,要再用打「開提醒 N」。
    這是為了守 LINE 免費額度 —— 條件一旦成立往往會**持續成立**(大盤跌破某點位後
    可能連跌好幾天),設「每日一次」的話每天都會再推一則。"""
    label = "大盤" if code == INDEX_CODE else code
    unit = "點" if code == INDEX_CODE else "元"
    title = (f"{label} {cond.replace('N點', '')}{threshold:g}{unit}"
             if cond.startswith("當日") else f"{label} {cond}{threshold:g}{unit}")
    props = {
        "Name": {"title": [{"text": {"content": title[:200]}}]},
        "標的": {"rich_text": [{"text": {"content": code}}]},
        "市場": {"select": {"name": market or "tse"}},
        "條件": {"select": {"name": cond}},
        "閾值": {"number": float(threshold)},
        "提醒內容": {"rich_text": [{"text": {"content": content[:1900]}}]},
        "啟用": {"checkbox": True},
        "重複方式": {"select": {"name": repeat}},
        "觸發次數": {"number": 0},
    }
    if stock_page_id:
        props["🔗關聯個股"] = {"relation": [{"id": stock_page_id}]}
    data = nt._post("/pages", {
        "parent": {"type": "data_source_id", "data_source_id": ALERT_DS},
        "properties": props,
    })
    return {"id": data.get("id", ""), "title": title}


def mark_fired(alert: dict, today_iso: str) -> None:
    props = {"最後觸發日": {"date": {"start": today_iso}},
             "觸發次數": {"number": (alert.get("fired") or 0) + 1}}
    if alert.get("repeat") == "只觸發一次":
        props["啟用"] = {"checkbox": False}
    nt._patch(f"/pages/{alert['id']}", {"properties": props})


# ---------------------------------------------------------------- 條件判斷

def evaluate(alert: dict, q: dict) -> tuple:
    """判斷是否觸發,回 (是否觸發, 說明字串)。"""
    price, prev = q.get("price"), q.get("prev_close")
    if price is None:
        return False, ""
    cond, th = alert["cond"], alert["threshold"]
    if th is None:
        return False, ""
    unit = "點" if alert["code"] == INDEX_CODE else "元"

    if cond in ("當日跌N點", "當日漲N點"):
        if prev is None:
            return False, ""
        diff = price - prev
        pct = diff / prev * 100 if prev else 0
        if cond == "當日跌N點" and -diff >= th:
            return True, f"{price:,.2f}({diff:+,.2f}{unit}, {pct:+.2f}%)"
        if cond == "當日漲N點" and diff >= th:
            return True, f"{price:,.2f}({diff:+,.2f}{unit}, {pct:+.2f}%)"
        return False, ""
    if cond == "跌破" and price <= th:
        return True, f"{price:,.2f}(已跌破 {th:g}{unit})"
    if cond == "漲破" and price >= th:
        return True, f"{price:,.2f}(已站上 {th:g}{unit})"
    return False, ""


def _push(text: str) -> bool:
    """推本人私訊(1:1 只算 1 則;推群組是按成員數計費)。回是否真的送出。"""
    today = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    if _pushed_today["date"] != today:
        _pushed_today.update({"date": today, "n": 0})
    if _pushed_today["n"] >= DAILY_PUSH_CAP:
        logger.warning("提醒推播已達每日上限 %d 則,略過", DAILY_PUSH_CAP)
        return False
    try:
        import calendar_notify
        if not calendar_notify.is_push_enabled():
            logger.info("主動推播已暫停,提醒只記 Notion")
            return False
    except Exception:  # noqa: BLE001
        pass
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    # ⚠️ 只推本人私訊,**不退回群組** —— 推群組是按成員數計費(3 人群推 1 則算 3 則),
    # 而且盤中提醒是給自己看的。沒設 LINE_ALLOWED_USER_ID 就寧可不推。
    target = os.environ.get("LINE_ALLOWED_USER_ID", "").strip()
    if not (token and target):
        logger.warning("未設 LINE_ALLOWED_USER_ID(本人私訊),略過提醒推播")
        return False
    resp = httpx.post("https://api.line.me/v2/bot/message/push",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json={"to": target, "messages": [{"type": "text", "text": text[:4900]}]},
                      timeout=20)
    if resp.status_code >= 300:
        logger.warning("提醒推播失敗 %s: %s", resp.status_code, resp.text[:200])
        return False
    _pushed_today["n"] += 1
    return True


# ---------------------------------------------------------------- 排程進入點

def check_alerts() -> dict:
    """盤中排程:抓一次報價,比對所有啟用中的提醒,觸發就推播。"""
    alerts = [a for a in list_alerts(only_enabled=True) if a["code"] and a["cond"]]
    if not alerts:
        return {"alerts": 0, "fired": 0}

    targets = sorted({(a["code"], a["market"]) for a in alerts})
    try:
        quotes = fetch_quotes(targets)
    except httpx.HTTPError as e:
        logger.warning("抓即時報價失敗:%s", e)
        return {"alerts": len(alerts), "fired": 0, "error": str(e)[:100]}

    today = datetime.datetime.now(TW_TZ)
    today_iso = today.strftime("%Y-%m-%d")
    today_compact = today.strftime("%Y%m%d")
    fired = 0
    for a in alerts:
        q = quotes.get(a["code"])
        if not q:
            continue
        # ⚠️ 非交易日 API 會回上一個交易日的報價,不比對日期的話週末會拿昨天的跌幅一直觸發
        if q.get("date") and q["date"] != today_compact:
            continue
        if a["repeat"] in ("每日一次", "只觸發一次") and a["last_fired"] == today_iso:
            continue
        hit, detail = evaluate(a, q)
        if not hit:
            continue
        label = "大盤" if a["code"] == INDEX_CODE else f"{a['code']} {q['name']}"
        tail = ("\n\n(這條已自動停用,要再用打「開提醒 N」)"
                if a["repeat"] == "只觸發一次" else "")
        text = (f"🔔 {a['title']}\n"
                f"{label} 現在 {detail}  {q.get('time', '')}\n"
                f"────────\n{a['content']}{tail}")
        _push(text)
        try:
            mark_fired(a, today_iso)
            fired += 1
        except nt.NotionError as e:
            logger.warning("更新提醒觸發狀態失敗:%s", e)
    if fired:
        logger.info("事件提醒:%d 條啟用,觸發 %d 條", len(alerts), fired)
    return {"alerts": len(alerts), "fired": fired}


# ---------------------------------------------------------------- LINE 指令

_USAGE = (
    "🔔 提醒用法\n"
    "提醒 大盤 跌 1000 打BBU、記憶體、3324\n"
    "提醒 2330 跌 100 台積電破線,回頭看一下權值\n"
    "提醒 3324 破 900 雙鴻跌到季線了\n"
    "提醒 6488 站上 950 環球晶突破前高\n"
    "\n"
    "格式:提醒 <大盤或代號> <跌|漲|破|站上> <數字> <要提醒自己的話>\n"
    "• 跌/漲 = 當日漲跌幅(以昨收為基準)\n"
    "• 破/站上 = 絕對價位\n"
    "\n"
    "列出:提醒清單    刪除:刪提醒 2    重新啟用:開提醒 2\n"
    "盤中每 15 分鐘檢查,觸發推你私訊 **一次** 後自動停用(省 LINE 額度)。"
)
# 容錯:空格可有可無(「大盤跌1000 打BBU」也通)、數字可帶逗號、後面可以接單位
_CMD_RE = re.compile(r"^提醒\s+(\S+?)\s*(跌|漲|破|站上)\s*([\d,\.]+)\s*(?:點|元|塊)?\s+(.+)$", re.S)
_COND_MAP = {"跌": "當日跌N點", "漲": "當日漲N點", "破": "跌破", "站上": "漲破"}


def is_alert_command(text: str) -> bool:
    t = (text or "").strip()
    first = t.splitlines()[0].strip() if t else ""
    return first.startswith(("提醒", "刪提醒", "開提醒", "重啟提醒"))


def run_alert_command(text: str) -> str:
    t = (text or "").strip()
    first = t.splitlines()[0].strip()

    if first.startswith("刪提醒"):
        arg = first[len("刪提醒"):].strip()
        alerts = list_alerts()
        if not arg.isdigit() or not (1 <= int(arg) <= len(alerts)):
            return f"用法:刪提醒 <編號>(目前共 {len(alerts)} 條,打「提醒清單」看編號)"
        a = alerts[int(arg) - 1]
        nt.archive_page(a["id"])
        return f"🗑️ 已刪除:{a['title']}"

    if first.startswith("開提醒") or first.startswith("重啟提醒"):
        arg = re.sub(r"^(開提醒|重啟提醒)", "", first).strip()
        alerts = list_alerts()
        if not arg.isdigit() or not (1 <= int(arg) <= len(alerts)):
            return f"用法:開提醒 <編號>(目前共 {len(alerts)} 條,打「提醒清單」看編號)"
        a = alerts[int(arg) - 1]
        # 一併清掉最後觸發日,否則同一天內重新啟用會被「今天推過了」擋掉
        nt._patch(f"/pages/{a['id']}",
                  {"properties": {"啟用": {"checkbox": True}, "最後觸發日": {"date": None}}})
        return f"🔔 已重新啟用:{a['title']}\n   → {a['content'][:60]}"

    arg = first[len("提醒"):].strip()
    if not arg or arg in ("用法", "說明", "help", "?"):
        return _USAGE
    if arg in ("清單", "列表", "list"):
        alerts = list_alerts()
        if not alerts:
            return "目前沒有設定任何提醒。打「提醒 用法」看怎麼設。"
        on = sum(1 for a in alerts if a["enabled"])
        lines = [f"🔔 事件提醒({len(alerts)} 條,{on} 條監看中)"]
        for i, a in enumerate(alerts, 1):
            if a["enabled"]:
                state = "🟢"
            else:
                state = f"⚪ 已觸發 {a['last_fired']}" if a["last_fired"] else "⚪ 停用中"
            lines.append(f"{i}. {state} {a['title']}\n   → {a['content'][:40]}")
        lines.append("重新啟用:開提醒 編號    刪除:刪提醒 編號")
        return "\n".join(lines)

    m = _CMD_RE.match(t.replace("　", " "))
    if not m:
        return _USAGE
    target, op, num, content = m.groups()
    threshold = float(num.replace(",", ""))

    if target in _INDEX_WORDS:
        code, stock_id = INDEX_CODE, ""
    elif re.fullmatch(r"\d{4,6}", target):
        code = target
        sp = nt.find_stock_page(code, "")
        stock_id = sp["id"] if sp else ""
    else:
        # 也接受個股名稱(要在主表裡才查得到代號)
        sp = nt.find_stock_page("", target)
        if not sp:
            return (f"看不懂標的「{target}」。用「大盤」或股號,例:提醒 2330 跌 100 …\n"
                    f"(用名稱的話,那檔要先在個股主表裡)")
        m2 = re.search(r"\d{3,6}", sp.get("label", ""))
        if not m2:
            return f"「{target}」在主表裡沒有代號,請改用股號。"
        code, stock_id = m2.group(0), sp["id"]

    market = resolve_market(code)
    try:
        q = fetch_quotes([(code, market)]).get(code, {})
    except httpx.HTTPError:
        q = {}
    res = add_alert(code, market, _COND_MAP[op], threshold, content.strip(), stock_id)
    now = ""
    if q.get("price") is not None:
        unit = "點" if code == INDEX_CODE else "元"
        now = f"\n目前 {q['price']:,.2f}{unit}(昨收 {q.get('prev_close', 0):,.2f})"
    return (f"✅ 已設定:{res['title']}{now}\n"
            f"觸發時會推你私訊:\n{content.strip()[:80]}\n"
            f"(盤中每 15 分鐘檢查,推一次後自動停用;要再用打「開提醒 編號」)")

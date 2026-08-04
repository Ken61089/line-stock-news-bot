"""
核心處理模組(分類驅動版):
  1. 依「第一行關鍵字」判斷分類(個股新聞 / 產業新聞 / 全球局勢 / 知識)
  2. 用該分類對應的方式請 Claude 做結構化整理
  3. 寫進該分類對應的 Google Sheet 分頁(分頁不存在會自動建立)

所有設定從環境變數讀取。
"""

import os
import re
import json
import threading
import datetime
from dataclasses import dataclass
from typing import List, Type, Callable

from openai import OpenAI
from pydantic import BaseModel, ValidationError, model_validator

from web_fetch import fetch_article, FetchError
import notion_timeline

# ==========================================================
# 設定
# ==========================================================
AI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://hnd1.aihub.zeabur.ai/v1")
AI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-5")

# 送進 AI 分析的內文字數上限(抓到的全文可能很長)
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "8000"))

# 概念題材種子清單:與 Notion 概念族群庫現有概念合併成 AI 標概念的白名單(見 _get_concept_whitelist)
_CONCEPT_SEED = [
    # AI / 伺服器 / 算力
    "AI伺服器", "AI ASIC", "CoWoS", "先進封裝", "HBM", "ABF載板", "液冷散熱",
    "伺服器電源", "BBU備援電池", "高速傳輸", "高速連接器", "銅纜(DAC)",
    # 半導體
    "晶圓代工", "先進製程", "成熟製程", "IC設計", "記憶體", "DRAM", "NAND",
    "封測", "矽智財(IP)", "半導體設備", "矽晶圓", "第三類半導體", "SiC", "GaN",
    # 光通訊 / 化合物
    "CPO", "矽光子", "光通訊", "光收發模組", "磷化銦(InP)", "砷化鎵(GaAs)", "CW Laser",
    # 消費電子 / PCB 供應鏈
    "蘋果概念", "手機供應鏈", "折疊機", "PCB", "軟板(FPC)", "被動元件", "連接器", "機殼",
    "銅箔基板", "玻纖布", "玻纖",
    # 電動車 / 車用
    "電動車", "車用電子", "車用半導體", "ADAS自駕", "充電樁", "車用PCB",
    # 機器人 / 自動化 / 無人機
    "人形機器人", "工業自動化", "機器視覺", "無人機", "軍工國防",
    # 網通 / 衛星
    "低軌衛星", "衛星通訊", "網通設備", "WiFi 7",
    # 綠能 / 重電 / 電力
    "重電", "智慧電網", "綠能", "太陽能", "離岸風電", "儲能", "氫能", "電力設備",
    # 生技醫療
    "新藥", "CDMO", "醫材", "減肥藥(GLP-1)", "生技",
    # 傳產 / 其他
    "散熱", "散裝航運", "貨櫃航運", "鋼鐵", "資產題材", "金融",
]

_URL_RE = re.compile(r"https?://\S+")

# 台灣時區(UTC+8,固定值;台灣不實施日光節約,雲端伺服器多為 UTC 故需明確指定)
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _now_str() -> str:
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

_openai_client = None


def _get_ai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        if not AI_API_KEY:
            raise RuntimeError("尚未設定 OPENAI_API_KEY(Zeabur AI Hub 的金鑰)")
        _openai_client = OpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)
    return _openai_client


# ---- AI token 自計量(持久化到 Notion,跨部署/跨月累計)----
# 每次呼叫 AI 從回應的 usage 累加當月 token,write-through 寫回 Notion「AI 用量統計」庫
# (一月一列)。開機時從 Notion 載回當月累計 → 部署重啟不歸零。Notion 掛了則退回純記憶體。
# 單價(每百萬 token,美金),換模型用 AI_PRICE_IN/OUT env 覆寫(如 Haiku 記得改)。
_AI_PRICE_IN = float(os.environ.get("AI_PRICE_IN", "3.0"))
_AI_PRICE_OUT = float(os.environ.get("AI_PRICE_OUT", "15.0"))
_ai_lock = threading.Lock()
_ai_state = {"month": "", "calls": 0, "prompt": 0, "completion": 0, "page_id": None, "loaded": False}


def _current_month() -> str:
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m")


def _month_cost(prompt: int, completion: int) -> float:
    return prompt / 1_000_000 * _AI_PRICE_IN + completion / 1_000_000 * _AI_PRICE_OUT


def _ensure_month_loaded() -> None:
    """確保 _ai_state 是「當月」的累計;跨月或首次會從 Notion 載回(呼叫端須持鎖)。"""
    m = _current_month()
    if _ai_state["loaded"] and _ai_state["month"] == m:
        return
    _ai_state.update({"month": m, "calls": 0, "prompt": 0, "completion": 0, "page_id": None})
    try:
        if notion_timeline.enabled():
            row = notion_timeline.read_ai_usage_month(m)
            if row:
                _ai_state.update({
                    "calls": row["calls"], "prompt": row["prompt"],
                    "completion": row["completion"], "page_id": row["page_id"],
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("讀取 AI 用量(Notion)失敗,改用記憶體:%s", e)
    _ai_state["loaded"] = True


def _ai_chat(**kwargs):
    """呼叫 AI Hub 並把 token 用量累加到當月、write-through 寫回 Notion。用法同 create。"""
    completion = _get_ai_client().chat.completions.create(**kwargs)
    try:
        u = completion.usage
        dp = int(getattr(u, "prompt_tokens", 0) or 0)
        dc = int(getattr(u, "completion_tokens", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return completion
    with _ai_lock:
        _ensure_month_loaded()
        _ai_state["calls"] += 1
        _ai_state["prompt"] += dp
        _ai_state["completion"] += dc
        try:  # write-through:計量失敗絕不影響 AI 主流程
            if notion_timeline.enabled():
                pid = notion_timeline.write_ai_usage_month(
                    _ai_state["month"], _ai_state["calls"], _ai_state["prompt"],
                    _ai_state["completion"],
                    _month_cost(_ai_state["prompt"], _ai_state["completion"]),
                    _ai_state["page_id"],
                )
                if pid:
                    _ai_state["page_id"] = pid
        except Exception as e:  # noqa: BLE001
            logger.warning("寫入 AI 用量(Notion)失敗:%s", e)
    return completion


def get_ai_usage() -> dict:
    """回傳當月累計 + 全部月份累計(讀 Notion)+ 估算美金。供「用量」報告用。"""
    with _ai_lock:
        _ensure_month_loaded()
        month, mc, mp, mco = (
            _ai_state["month"], _ai_state["calls"],
            _ai_state["prompt"], _ai_state["completion"],
        )
    persisted = False
    allc, allp, allco, months = mc, mp, mco, 1
    try:
        if notion_timeline.enabled():
            s = notion_timeline.read_ai_usage_all()
            allc, allp, allco, months = s["calls"], s["prompt"], s["completion"], s["months"]
            persisted = True
    except Exception as e:  # noqa: BLE001
        logger.warning("讀取 AI 累計(Notion)失敗:%s", e)
    return {
        "model": AI_MODEL,
        "month": month,
        "month_calls": mc, "month_prompt": mp, "month_completion": mco,
        "month_cost": _month_cost(mp, mco),
        "all_calls": allc, "all_prompt": allp, "all_completion": allco,
        "all_cost": _month_cost(allp, allco), "all_months": months,
        "persisted": persisted,
    }


# ==========================================================
# 容錯:把 AI 可能回的物件/數字統一轉乾淨字串
# ==========================================================
def _flatten_to_str(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        code = item.get("stock_id") or item.get("code") or item.get("id") or item.get("symbol") or ""
        name = item.get("stock_name") or item.get("name") or item.get("group_name") or item.get("group") or ""
        combined = f"{code} {name}".strip()
        if combined:
            return combined
        parts = [str(x).strip() for x in item.values() if isinstance(x, (str, int, float)) and str(x).strip()]
        return " ".join(parts)
    return str(item).strip()


def _coerce_str_list(v) -> List[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    return [s for s in (_flatten_to_str(x) for x in v) if s]


class _CoercedModel(BaseModel):
    """所有模型的基底:自動把 str 欄位、List[str] 欄位的內容轉乾淨,不論 AI 怎麼回都不崩。"""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            if name not in out:
                continue
            ann = field.annotation
            if ann is str:
                out[name] = _flatten_to_str(out[name]) if out[name] is not None else ""
            elif ann == List[str]:
                out[name] = _coerce_str_list(out[name])
        return out


# ==========================================================
# 各分類的資料模型
# ==========================================================
class TimelineEvent(_CoercedModel):
    date: str = ""
    event: str = ""


class TimelineInput(_CoercedModel):
    """LINE「時程」指令解析結果:一句話 → 一筆時間軸事件。"""
    stock_code: str = ""    # 台股代號(3-6 位數),沒有就空
    stock_name: str = ""    # 個股名稱
    title: str = ""         # 事件標題(精簡)
    date_start: str = ""    # YYYY-MM-DD
    date_end: str = ""      # YYYY-MM-DD(區間才有,如擴廠→量產)
    event_type: str = ""    # 需為時程庫合法類型之一
    note: str = ""          # 補充備註


class StockNews(_CoercedModel):
    summary: str = ""
    market: str = ""
    mentioned_stocks: List[str] = []
    concept_groups: List[str] = []
    timelines: List[TimelineEvent] = []


class IndustryNews(_CoercedModel):
    summary: str = ""
    industry_groups: List[str] = []
    key_trends: List[str] = []
    mentioned_stocks: List[str] = []


class GlobalNews(_CoercedModel):
    summary: str = ""
    topics: List[str] = []
    affected_markets: List[str] = []
    timelines: List[TimelineEvent] = []


class KnowledgeNote(_CoercedModel):
    topic: str = ""
    key_points: List[str] = []
    keywords: List[str] = []


class IndustryReport(_CoercedModel):
    stocks: List[str] = []          # 報告涵蓋的個股
    concept_groups: List[str] = []  # 概念股/題材族群(如 CPO、磷化銦、光通訊)
    report_date: str = ""           # 報告日期
    broker: str = ""                # 出具報告的券商/機構
    target_price: str = ""          # 券商目標價
    recent_revenue: str = ""        # 近期營收狀況
    timelines: List[TimelineEvent] = []  # 關鍵時程
    summary: str = ""               # 報告總結
    catalysts: List[str] = []       # 利多訊號(帶動營收/轉型的字眼)
    risks: List[str] = []           # 利空訊號(反向字眼)


# ==========================================================
# 格式化小工具
# ==========================================================
def _fmt_timeline(timelines: List[TimelineEvent]) -> str:
    return "\n".join(f"[{i}] {t.date} -> {t.event}" for i, t in enumerate(timelines, 1)).strip()


def _fmt_bullets(items: List[str]) -> str:
    return "\n".join(f"• {x}" for x in items).strip()


def _join(items: List[str]) -> str:
    return ", ".join(items)


def _fmt_report_summary(a: "IndustryReport") -> str:
    """報告總結欄:總結 + 明確標出利多/利空訊號,方便日後篩選。"""
    parts = []
    if a.summary:
        parts.append(a.summary)
    if a.catalysts:
        parts.append("【利多訊號】\n" + _fmt_bullets(a.catalysts))
    if a.risks:
        parts.append("【利空訊號】\n" + _fmt_bullets(a.risks))
    return "\n\n".join(parts).strip()


# 概念/族群標籤的共用精準規則(附加在會抽概念的分類提示後,降低亂標、統一名稱)
_CONCEPT_RULE = (
    " 【概念/族群標籤務求精準,寧缺勿濫】只標內文有明確依據、且確實是該個股核心投資題材的概念或供應鏈族群;"
    "不要硬塞僅順帶提到、或過於空泛的大方向(例如單獨的『AI』『科技股』『半導體』『景氣復甦』這類太籠統的詞;"
    "但具體題材如『AI伺服器』『矽光子』則可以)。"
    "名稱一律用業界慣用的精簡標準寫法(如 CPO、矽光子、磷化銦、HBM、先進封裝、散熱),"
    "且同一題材每次都用同一個固定名稱(例:一律寫『CPO』,不要一下寫『CPO』一下寫『共同封裝光學』),以利後續比對。"
    " 【供應鏈定位優先】標概念時,要以『這檔公司自身的產品、以及它在供應鏈所處的位置』為主要標籤、並排在最前面。"
    "例如公司是做玻纖布(上游材料)的,就先標『玻纖布、銅箔基板、PCB』這些它所屬的層級;"
    "不要只因為新聞提到它受惠某終端需求,就把該終端(如 AI伺服器、資料中心)當成它的主要概念——否則所有股票都會變成 AI伺服器概念股,失去分辨力。"
    "它供應或受惠的下游應用『可以』在後面補充標註以保留供應鏈關聯,但屬於次要、要排在自身層級之後,不可取而代之;"
    "唯有當公司本身就是生產該終端產品(例如伺服器整機廠)時,才把該終端當成主要概念。"
)


# ==========================================================
# Notion 寫入(取代 Google Sheet;每個分類一個 writer,回傳 (ok, 附註字串))
# ==========================================================
_AUTO_CREATE_STOCK = os.environ.get("AUTO_CREATE_STOCK", "1") != "0"


def _valid_iso(s: str) -> str:
    s = (s or "")[:10]
    try:
        datetime.date.fromisoformat(s)
        return s
    except (ValueError, TypeError):
        return ""


def _earliest_iso_date(timelines) -> str:
    """從 timelines 挑一個合法日期當事件關鍵日期:優先最近的未來日期,否則最早的。"""
    today = datetime.datetime.now(TW_TZ).date()
    valid = [d for d in (_valid_iso(getattr(t, "date", "")) for t in (timelines or [])) if d]
    if not valid:
        return ""
    future = [d for d in valid if datetime.date.fromisoformat(d) >= today]
    return min(future) if future else min(valid)


_STOCK_CODE_RE = re.compile(r"\d{3,6}")


def _is_non_stock_code(s: str) -> bool:
    """個股字串的代號為 5~6 位數(可轉債 CB、ETF 等,非一般四碼現股)→ 不自動建檔。"""
    m = _STOCK_CODE_RE.search(s or "")
    return bool(m) and len(m.group(0)) >= 5


def _resolve_stocks(stock_strs):
    """['2330 台積電', ...] → Notion 個股頁 ids。找不到才視情況自動新增:
    一般四碼現股(或無代號的名稱)會建檔;5~6 碼(CB/ETF 等非現股)不建檔。
    回傳 (ids, warns, skipped);skipped 為未建檔的 CB/非現股字串。"""
    ids, warns, skipped = [], [], []
    for s in stock_strs or []:
        s = (s or "").strip()
        if not s:
            continue
        page = None
        try:
            page = notion_timeline.find_stock_page("", s)
        except notion_timeline.NotionError as e:
            logger.warning("查個股頁失敗:%s", e)
        if page is None:
            # 個股主表查無此檔:CB/非現股(5~6碼)不建檔;其餘(四碼現股或名稱)才自動建
            if _is_non_stock_code(s):
                skipped.append(s)
                continue
            if _AUTO_CREATE_STOCK:
                try:
                    page = notion_timeline.create_stock_page("", s)
                    warns.append(f"🆕 已自動新增個股「{page['label']}」到個股主表")
                except notion_timeline.NotionError as e:
                    logger.warning("自動新增個股失敗:%s", e)
        if page and page.get("id"):
            ids.append(page["id"])
    return list(dict.fromkeys(ids)), warns, skipped


def _write_news_event(title, url, *, event_type, stocks, concept_names, note, date_start=""):
    """個股新聞/產業新聞/個股產業報告共用:寫一筆時程庫記錄、關聯所有個股與概念。回傳附註字串。"""
    if url:
        try:
            if notion_timeline.find_news_by_link(notion_timeline.TIMELINE_DS, url):
                return "(此連結先前已記錄過,略過重複寫入)"
        except notion_timeline.NotionError as e:
            logger.warning("查重失敗:%s", e)
    ids, warns, skipped = _resolve_stocks(stocks)
    # CB/非現股不建檔,但仍把它保留在內文,資訊不遺失
    if skipped:
        note = f"{note}\n\n提及(未建檔·CB/非現股):{', '.join(skipped)}".strip()
        warns.append(f"ℹ️ 未建檔(5~6碼 CB/非現股):{', '.join(skipped)}")
    concept_ids = []
    try:
        concept_ids = notion_timeline.ensure_concepts(concept_names)
    except notion_timeline.NotionError as e:
        logger.warning("建立概念失敗:%s", e)
    notion_timeline.add_event(
        title=title, date_start=date_start, event_type=event_type,
        stock_page_ids=ids, concept_ids=concept_ids, note=note,
        source="新聞bot", link=url,
    )
    for sid in ids:
        try:
            notion_timeline.link_stock_concepts(sid, concept_ids)
        except notion_timeline.NotionError as e:
            logger.warning("關聯個股概念失敗:%s", e)
    return "\n".join(warns)


def _notion_individual(a, title, url, now):
    note = a.summary
    if a.market:
        note = f"市場:{a.market}\n{note}".strip()
    tl = _fmt_timeline(a.timelines)
    if tl:
        note = f"{note}\n\n【關鍵時程】\n{tl}".strip()
    add = _write_news_event(
        title, url, event_type="個股新聞", stocks=a.mentioned_stocks,
        concept_names=a.concept_groups, note=note, date_start=_earliest_iso_date(a.timelines),
    )
    return True, add


def _notion_industry(a, title, url, now):
    note = a.summary
    kt = _fmt_bullets(a.key_trends)
    if kt:
        note = f"{note}\n\n【重點趨勢】\n{kt}".strip()
    add = _write_news_event(
        title, url, event_type="產業新聞", stocks=a.mentioned_stocks,
        concept_names=a.industry_groups, note=note,
    )
    return True, add


def _notion_report(a, title, url, now):
    head = [x for x in (
        f"券商:{a.broker}" if a.broker else "",
        f"目標價:{a.target_price}" if a.target_price else "",
        f"近期營收:{a.recent_revenue}" if a.recent_revenue else "",
    ) if x]
    tl = _fmt_timeline(a.timelines)
    parts = [" | ".join(head), _fmt_report_summary(a), (f"【時間軸】\n{tl}" if tl else "")]
    note = "\n\n".join(p for p in parts if p).strip()
    date_start = _valid_iso(a.report_date) or _earliest_iso_date(a.timelines)
    add = _write_news_event(
        title, url, event_type="個股產業報告", stocks=a.stocks,
        concept_names=a.concept_groups, note=note, date_start=date_start,
    )
    return True, add


def _notion_global(a, title, url, now):
    if url:
        try:
            if notion_timeline.find_news_by_link(notion_timeline.GLOBAL_DS, url):
                return True, "(此連結先前已記錄過,略過重複寫入)"
        except notion_timeline.NotionError as e:
            logger.warning("查重失敗:%s", e)
    notion_timeline.add_global(
        title=title, summary=a.summary, topics=a.topics, markets=a.affected_markets,
        date_start=_earliest_iso_date(a.timelines), note=_fmt_timeline(a.timelines), link=url,
    )
    return True, ""


def _notion_knowledge(a, title, url, now):
    if url:
        try:
            if notion_timeline.find_news_by_link(notion_timeline.KNOWLEDGE_DS, url):
                return True, "(此連結先前已記錄過,略過重複寫入)"
        except notion_timeline.NotionError as e:
            logger.warning("查重失敗:%s", e)
    notion_timeline.add_knowledge(
        topic=(a.topic or title), key_points_text=_fmt_bullets(a.key_points),
        keywords=a.keywords, link=url,
    )
    return True, ""


# ==========================================================
# 分類設定(關鍵字 → 模型 / 提示 / Notion 寫入 / 回覆)
# ==========================================================
@dataclass
class CategoryConfig:
    label: str
    tab: str
    header: List[str]
    model: Type[_CoercedModel]
    schema_hint: str
    task: str
    to_row: Callable
    format_reply: Callable
    # 回傳 [(個股字串, [概念標籤...]), ...] 供更新「概念股主表」;None = 此分類不參與
    to_concept_pairs: Callable = None
    # 把分析結果寫進 Notion,回傳 (ok, 附註字串);取代舊 to_row/Sheet 寫入
    to_notion: Callable = None


INDIVIDUAL = CategoryConfig(
    label="個股新聞",
    tab="新聞時程動態庫",
    header=["處理時間", "新聞標題", "AI 核心摘要", "提及個股", "概念族群分類", "關鍵時程與事件", "新聞原文/連結", "市場/地區"],
    model=StockNews,
    schema_hint='{"summary":"60-100字摘要","market":"主要市場,如 台股/美股/日股/港股,多個用、分隔","mentioned_stocks":["6442 光聖"],"concept_groups":["CPO","矽光子"],"timelines":[{"date":"2026-07-15","event":"可轉債掛牌"}]}',
    task="1.撰寫60-100字投資人摘要。2.判斷主要涉及的股票市場/地區(台股/美股/日股/港股等)。3.精確提取個股與代號。4.辨別相關科技概念股/供應鏈族群(concept_groups)。5.抽取所有未來關鍵時程。" + _CONCEPT_RULE,
    to_row=lambda a, title, url, now: [now, title, a.summary, _join(a.mentioned_stocks), _join(a.concept_groups), _fmt_timeline(a.timelines), url, a.market],
    format_reply=lambda a: (
        "✅ 已寫入【個股新聞】\n\n"
        f"📌 摘要:{a.summary}\n"
        f"🌍 市場:{a.market or '(未標明)'}\n"
        f"📈 個股:{_join(a.mentioned_stocks) or '(無)'}\n"
        f"🏷️ 族群:{_join(a.concept_groups) or '(無)'}\n"
        f"🗓️ 時程:\n{_fmt_timeline(a.timelines) or '  (無明確時程)'}"
    ),
    to_concept_pairs=lambda a: [(s, a.concept_groups) for s in a.mentioned_stocks],
    to_notion=_notion_individual,
)

INDUSTRY = CategoryConfig(
    label="產業新聞",
    tab="產業動態",
    header=["處理時間", "新聞標題", "AI 核心摘要", "相關產業/族群", "重點趨勢", "提及個股", "新聞原文/連結"],
    model=IndustryNews,
    schema_hint='{"summary":"60-100字摘要","industry_groups":["先進封裝","散熱"],"key_trends":["趨勢一","趨勢二"],"mentioned_stocks":["2330 台積電"]}',
    task="這是產業新聞。1.撰寫60-100字摘要。2.辨別相關產業/供應鏈族群(industry_groups)。3.整理出重點趨勢(條列,每點一句)。4.提取文中提及的個股。" + _CONCEPT_RULE,
    to_row=lambda a, title, url, now: [now, title, a.summary, _join(a.industry_groups), _fmt_bullets(a.key_trends), _join(a.mentioned_stocks), url],
    format_reply=lambda a: (
        "✅ 已寫入【產業新聞】\n\n"
        f"📌 摘要:{a.summary}\n"
        f"🏭 產業/族群:{_join(a.industry_groups) or '(無)'}\n"
        f"📊 重點趨勢:\n{_fmt_bullets(a.key_trends) or '  (無)'}\n"
        f"📈 個股:{_join(a.mentioned_stocks) or '(無)'}"
    ),
    to_concept_pairs=lambda a: [(s, a.industry_groups) for s in a.mentioned_stocks],
    to_notion=_notion_industry,
)

GLOBAL = CategoryConfig(
    label="全球局勢",
    tab="全球局勢動態",
    header=["處理時間", "新聞標題", "AI 核心摘要", "影響主題", "可能受影響市場/資產", "關鍵時程", "新聞原文/連結"],
    model=GlobalNews,
    schema_hint='{"summary":"60-100字摘要","topics":["升息","關稅"],"affected_markets":["美股","原油","台幣匯率"],"timelines":[{"date":"2026-07","event":"FOMC會議"}]}',
    task="這是全球總經/地緣局勢新聞(如升息、油價、關稅、戰爭、央行政策等)。1.撰寫60-100字摘要。2.列出主要影響主題。3.判斷可能受影響的市場或資產類別(股市/債市/匯率/原物料/特定區域)。4.抽取關鍵時程(會議、生效日等)。",
    to_row=lambda a, title, url, now: [now, title, a.summary, _join(a.topics), _join(a.affected_markets), _fmt_timeline(a.timelines), url],
    format_reply=lambda a: (
        "✅ 已寫入【全球局勢】\n\n"
        f"📌 摘要:{a.summary}\n"
        f"🌐 影響主題:{_join(a.topics) or '(無)'}\n"
        f"💱 受影響市場/資產:{_join(a.affected_markets) or '(無)'}\n"
        f"🗓️ 時程:\n{_fmt_timeline(a.timelines) or '  (無明確時程)'}"
    ),
    to_notion=_notion_global,
)

KNOWLEDGE = CategoryConfig(
    label="知識",
    tab="知識補充庫",
    header=["處理時間", "主題", "重點整理", "關鍵字", "原文連結"],
    model=KnowledgeNote,
    schema_hint='{"topic":"這份資料的主題","key_points":["重點一","重點二"],"keywords":["關鍵字1","關鍵字2"]}',
    task="這是知識/觀念補充資料。1.歸納出主題。2.整理重點(條列,每點一句白話)。3.列出關鍵字。不需要個股或時程。",
    to_row=lambda a, title, url, now: [now, a.topic or title, _fmt_bullets(a.key_points), _join(a.keywords), url],
    format_reply=lambda a: (
        "✅ 已寫入【知識補充庫】\n\n"
        f"📚 主題:{a.topic}\n"
        f"📝 重點整理:\n{_fmt_bullets(a.key_points) or '  (無)'}\n"
        f"🔖 關鍵字:{_join(a.keywords) or '(無)'}"
    ),
    to_notion=_notion_knowledge,
)

REPORT = CategoryConfig(
    label="產業報告",
    tab="個股產業報告",
    header=["處理時間", "個股", "概念股", "報告日期", "出具券商", "券商目標價", "近期營收", "時間軸", "報告總結", "報告原文/連結"],
    model=IndustryReport,
    schema_hint='{"stocks":["2455 全新"],"concept_groups":["磷化銦","光通訊","CPO","矽光子"],"report_date":"2026-06-27","broker":"摩根士丹利","target_price":"473元","recent_revenue":"5月營收月增0.69%、年增46.51%","timelines":[{"date":"2026-07-16","event":"法說會"}],"summary":"80-150字報告總結","catalysts":["產品調漲雙位數","切入美系客戶供應鏈","擴產"],"risks":["PA出貨不如預期","資料中心需求趨緩"]}',
    task=(
        "這是券商/分析師出具的個股研究報告(或法人報告、產業深度報告)。請結構化整理:"
        "1.報告涵蓋的個股與代號(stocks)。"
        "2.這檔屬於哪些概念股/題材族群(concept_groups),即報告主角所屬的投資題材,"
        "例如 CPO、矽光子、磷化銦、光通訊、先進封裝、散熱、AI伺服器、機器人等(可多個,沒有就留空)。"
        "3.報告日期(report_date,YYYY-MM-DD;若只寫月/日,用今天日期推斷正確年份)。"
        "4.出具報告的券商/機構(broker)。"
        "5.券商給的目標價(target_price,含單位,如「1200元」;沒有就留空)。"
        "6.近期營收狀況(recent_revenue,如月營收年增率、季營收、毛利率等具體數字)。"
        "7.關鍵時程(timelines,如法說會、新廠投產、新品量產、訂單交付)。"
        "8.報告總結(summary,80-150字,寫給投資人看的重點)。"
        "9.【特別重要】利多訊號(catalysts):請對任何看起來會『帶動營收成長或公司轉型』的字眼高度敏感、寧多勿漏,"
        "例如:漲價/調漲/報價上揚、新增客戶/拿下大單、打入或切入某供應鏈/通過認證、擴產/擴廠/擴充產能、"
        "資本支出增加/上修、轉型、產能滿載/利用率提升、訂單能見度高、急單、供不應求、毛利率提升、新產品/新應用等。"
        "10.利空訊號(risks):同樣對反向字眼敏感,例如:降價/殺價/報價下滑、砍單/掉單、客戶流失/轉單、"
        "產能利用率下降、資本支出縮減/遞延、需求疲弱、庫存調整去化、毛利率下滑、認證未過/出貨遞延等。"
        "catalysts 與 risks 都用簡短詞組條列,每點抓住關鍵(可帶一點原文數字)。"
    ) + _CONCEPT_RULE,
    to_row=lambda a, title, url, now: [
        now, _join(a.stocks), _join(a.concept_groups), a.report_date, a.broker, a.target_price,
        a.recent_revenue, _fmt_timeline(a.timelines), _fmt_report_summary(a), url,
    ],
    format_reply=lambda a: (
        "✅ 已寫入【產業報告】\n\n"
        f"📈 個股:{_join(a.stocks) or '(未標明)'}\n"
        f"🏷️ 概念股:{_join(a.concept_groups) or '(無)'}\n"
        f"🏦 券商:{a.broker or '(未標明)'}\n"
        f"🎯 目標價:{a.target_price or '(無)'}\n"
        f"💰 近期營收:{a.recent_revenue or '(無)'}\n"
        f"📅 報告日期:{a.report_date or '(無)'}\n"
        f"📝 總結:{a.summary or '(無)'}\n"
        f"🟢 利多:{_join(a.catalysts) or '(無)'}\n"
        f"🔴 利空:{_join(a.risks) or '(無)'}\n"
        f"🗓️ 時間軸:\n{_fmt_timeline(a.timelines) or '  (無明確時程)'}"
    ),
    to_concept_pairs=lambda a: [(s, a.concept_groups) for s in a.stocks],
    to_notion=_notion_report,
)

# 關鍵字 → 分類(含別名);依長度由長到短比對,避免「個股」先吃掉「個股新聞」
_KEYWORDS = [
    ("個股新聞", INDIVIDUAL), ("個股", INDIVIDUAL),
    ("產業報告", REPORT), ("個股報告", REPORT), ("券商報告", REPORT), ("研究報告", REPORT), ("法人報告", REPORT),
    ("產業新聞", INDUSTRY), ("產業", INDUSTRY),
    ("全球局勢新聞", GLOBAL), ("全球局勢", GLOBAL), ("全球", GLOBAL), ("總經", GLOBAL), ("國際", GLOBAL),
    ("知識補充", KNOWLEDGE), ("知識", KNOWLEDGE), ("筆記", KNOWLEDGE), ("觀念", KNOWLEDGE),
]
_KEYWORDS.sort(key=lambda kv: len(kv[0]), reverse=True)

GUIDANCE = (
    "⚠️ 請在第一行標上分類關鍵字,第二行起貼內容(或直接貼一個新聞連結)。\n\n"
    "可用分類:\n"
    "• 個股新聞\n• 產業新聞\n• 產業報告(券商研究報告)\n• 全球局勢\n• 知識(或筆記)\n\n"
    "範例:\n個股新聞\n光聖(6442)受惠CPO需求爆發…\n\n"
    "📎 也可以只貼連結,我會自動抓全文整理。\n"
    "🔍 想查資料庫?用「查」開頭,例如:查 光聖最近的時程\n"
    "🗓️ 記時程?用「時程」開頭,例如:時程 8131福懋科 8月量產\n"
    "👀 隔日盯盤?用「關注」開頭,例如:關注 2330 台積電 站上季線再追\n"
    "🛠️ 想更正?「主表」改概念股主表、「改」改某筆新聞、「改股 8111:6442 光聖」一次改遍所有分頁\n"
    "📖 打「說明」看完整指令總表"
)

# 查詢模式的觸發前綴(訊息開頭出現就進查詢,而非寫入)
_QUERY_PREFIXES = ["查詢", "查", "問", "搜尋"]
# 查詢功能開關:資料庫完善前先停用。要恢復就在環境變數設 ENABLE_QUERY=1
QUERY_ENABLED = os.environ.get("ENABLE_QUERY", "0") == "1"

# 時程事件的觸發前綴(訊息開頭出現就寫進 Notion 時間軸,而非 Google Sheet)
_TIMELINE_PREFIXES = ["時程", "事件", "行事曆"]

# 隔日盯盤筆記的觸發前綴(寫進 Notion,事件類型「每日關注」,隔天 08:30 隨推播帶出)
_WATCHLIST_PREFIXES = ["關注"]
_WATCHLIST_TYPE = "每日關注"
_WATCHLIST_USAGE = (
    "👀 關注用法:「關注」開頭,列出隔天想盯的股票(可多行,一行一檔),"
    "隔天早上 08:30 會隨推播通知一次。\n"
    "例如:\n"
    "關注\n"
    "2330 台積電 站上季線再追\n"
    "3661 世芯 觀察缺口\n"
    "(想指定今天請用「關注 今天 ...」;預設是隔天)\n"
    "📌 同一天再打一次「關注」= 整批更新成最新版(會蓋掉當天舊的)\n"
    "\n"
    "查看/修改(先打「明日關注」看編號):\n"
    "• 明日關注 / 今日關注   列出該日清單\n"
    "• 刪關注 2(刪明日第2筆,可多筆 刪關注 2 4;不帶編號=刪最新)\n"
    "• 改關注 2 新內容(改明日第2筆)"
)
# 關注的查看(整段字比對)與刪改(針對「明日」= 關注預設存入日)
_WATCHLIST_TODAY_WORDS = {"今日關注", "今天關注", "本日關注"}
_WATCHLIST_TMR_WORDS = {"明日關注", "明天關注"}
_WATCHLIST_DELETE_PREFIXES = ("刪關注", "刪除關注", "刪掉關注", "移除關注")
_WATCHLIST_EDIT_PREFIXES = ("改關注", "修改關注", "編輯關注")

# 隨手筆記:團隊隨時記,當天 21:00 隨推播彙整;「今日隨筆」可即時叫出當天已記的
_NOTE_PREFIXES = ("隨筆", "隨手筆記", "隨手記", "備忘")
_NOTE_TYPE = "隨手筆記"
_TODAY_NOTES_WORDS = {"今日隨筆", "今天隨筆", "本日隨筆", "今日筆記", "今天筆記"}
# 刪除/修改今天的隨筆(用「今日隨筆」列表的編號指定)
_NOTE_DELETE_PREFIXES = ("刪隨筆", "刪除隨筆", "刪掉隨筆", "移除隨筆")
_NOTE_EDIT_PREFIXES = ("改隨筆", "修改隨筆", "編輯隨筆")
_NOTE_USAGE = (
    "📝 隨筆用法:「隨筆」後面接想記的內容(可多行),例如:\n"
    "隨筆 台積電 CoWoS 產能吃緊,留意設備股\n"
    "當天的隨筆會在晚上 21:00 隨推播彙整;打「今日隨筆」可即時叫出當天已記的。\n"
    "\n"
    "打錯要改/刪(先打「今日隨筆」看編號):\n"
    "• 刪隨筆 2      刪掉第 2 則(可一次多筆:刪隨筆 2 4)\n"
    "• 刪隨筆        不帶編號=刪掉最新一則\n"
    "• 改隨筆 2 新內容   把第 2 則改成新內容"
)

# 「說明」指令:整段訊息完全等於這些字才觸發(避免新聞內文誤中)
_HELP_WORDS = {"說明", "help", "指令", "幫助", "用法", "選單", "menu", "?", "？"}
_HELP_TEXT = (
    "📖 機器人指令總覽\n"
    "\n"
    "━━ 自動推播時間(台灣) ━━\n"
    "• 每天 08:30 今日行事曆＋法說會倒數(前3天)\n"
    "• 週日 21:00 下週行事曆\n"
    "(每天 06:20/06:30 自動抓美國經濟事件、法說會)\n"
    "• 🚨 每 2 小時監看重大訊息,追蹤股命中就彙整成一則警示\n"
    "\n"
    "━━ 存新聞 ━━\n"
    "第一行打分類,第二行貼內容或連結:\n"
    "• 個股新聞  • 產業新聞  • 產業報告\n"
    "• 全球局勢  • 知識(或筆記)\n"
    "(只貼連結我會自動抓全文)\n"
    "\n"
    "━━ 記時程 ━━(「時程」開頭,寫進 Notion 時間軸)\n"
    "• 時程 8131福懋科 8月量產\n"
    "• 時程 2330 7/17 法說會\n"
    "• 時程 3583 擴廠 8月試產、11月量產\n"
    "• 批次:第一行只打「時程」,之後一行一筆\n"
    "\n"
    "━━ 隔日盯盤 ━━(「關注」開頭,隔天 08:30 隨推播通知)\n"
    "• 關注 2330 台積電 站上季線再追\n"
    "• 多檔:「關注」換行後一行一檔\n"
    "• 明日關注 / 今日關注(列出清單、有編號)\n"
    "• 刪關注 2、改關注 2 新內容(針對明日)\n"
    "\n"
    "━━ 隨手筆記 ━━(隨時記,當天 21:00 隨推播彙整)\n"
    "• 隨筆 台積電CoWoS產能吃緊,留意設備股\n"
    "• 打「今日隨筆」叫出今天已記的(有編號)\n"
    "• 刪隨筆 2(刪第2則,不帶編號=刪最新)\n"
    "• 改隨筆 2 新內容(改第2則)\n"
    "\n"
    "━━ 查行事曆 ━━(唯讀,列出某段期間的事件)\n"
    "• 早報(今日行事曆＋法說會預告,同 08:30 內容)\n"
    "• 本週 / 下週\n"
    "• 列出 7/10(某天)  • 列出 7/10~7/15(範圍)\n"
    "\n"
    "━━ 查重訊 ━━(唯讀,查追蹤股累積的重大訊息)\n"
    "• 查重訊本週  • 查重訊 7/10  • 查重訊 7/10~7/15\n"
    "\n"
    "━━ 推播開關 ━━(僅本人;省 LINE 額度用)\n"
    "• 推播關閉:暫停早報/週報/MOPS 推播(改打「早報/下週/查重訊今天」查)\n"
    "• 推播開啟:恢復  • 推播狀態:查目前狀態\n"
    "\n"
    "━━ 用量 ━━(唯讀,即時看成本)\n"
    "• 打「用量」看 LINE 推播則數 / Firecrawl credit / AI token\n"
    "\n"
    "━━ 法說會自動更新 ━━(每天自動抓,也可手動)\n"
    "• 打「更新法說會」→ 立即抓追蹤股的法說會進時程\n"
    "\n"
    "━━ 美國經濟事件 ━━(每天自動抓 MacroMicro,也可手動)\n"
    "• 打「更新經濟」→ 立即抓美國經濟事件(CPI/FOMC…)進時程\n"
    "\n"
    "━━ 改錯:股號打錯 ━━\n"
    "• 改股 1514:1815 富喬 → 所有分頁一次改\n"
    "• 主表 改股 1514:1815 富喬 → 只改主表\n"
    "\n"
    "━━ 新增追蹤標的 ━━\n"
    "• 主表 加股 3529 力旺(只建檔,不掛概念不建事件)\n"
    "\n"
    "━━ 改錯:概念股主表 ━━\n"
    "• 主表 加 CPO:6442 光聖\n"
    "• 主表 移除 光通訊:2330\n"
    "• 主表 移除股 8111(從所有概念移除)\n"
    "• 主表 合併 共同封裝光學:CPO\n"
    "• 主表 刪除 半導體\n"
    "\n"
    "━━ 改錯:某一列新聞 ━━\n"
    "• 改 產業報告 全新 目標價:500元\n"
    "  (分類用關鍵字,不是分頁名)\n"
    "\n"
    "💡 打「主表」或「改」不帶參數,會顯示該類詳細用法。"
)


def _is_help(text: str) -> bool:
    return text.strip().lower() in _HELP_WORDS


def is_group_additive_command(text: str) -> bool:
    """群組成員可用的「新增/查詢類」指令才回 True(時程/關注/更新法說會/存新聞/查詢/說明);
    破壞性操作(改股、主表修正、刪除)與一般聊天回 False,群組不處理。"""
    s = (text or "").strip()
    if not s:
        return False
    if s.lower() in _HELP_WORDS:
        return True
    if s in _TODAY_NOTES_WORDS or s in _WATCHLIST_TODAY_WORDS or s in _WATCHLIST_TMR_WORDS:
        return True
    first = s.splitlines()[0].strip()
    additive_prefixes = (
        tuple(_TIMELINE_PREFIXES) + tuple(_WATCHLIST_PREFIXES)
        + tuple(_WATCHLIST_DELETE_PREFIXES) + tuple(_WATCHLIST_EDIT_PREFIXES)
        + tuple(_NOTE_PREFIXES) + tuple(_NOTE_DELETE_PREFIXES)
        + tuple(_NOTE_EDIT_PREFIXES) + tuple(_FETCH_EARNINGS_PREFIXES)
        + tuple(_FETCH_ECON_PREFIXES)
    )
    if QUERY_ENABLED:
        additive_prefixes += tuple(_QUERY_PREFIXES)
    if first.startswith(additive_prefixes):
        return True
    # 存新聞:第一行是分類關鍵字(個股新聞/產業新聞/…)
    cfg, _ = detect_category(text)
    return cfg is not None


class NoCategoryError(Exception):
    """第一行沒有可辨識的分類關鍵字,或關鍵字後沒有內容。"""


@dataclass
class Result:
    label: str
    reply: str


def detect_category(text: str):
    """回傳 (CategoryConfig 或 None, 去掉關鍵字後的內容)。"""
    lines = text.strip().splitlines()
    if not lines:
        return None, ""
    first = lines[0].strip()
    for kw, cfg in _KEYWORDS:
        if first.startswith(kw):
            remainder = first[len(kw):].strip(" :：-、,，.。　")
            rest = "\n".join(lines[1:])
            content = (remainder + ("\n" + rest if rest else "")).strip() if remainder else rest.strip()
            return cfg, content
    return None, text


# ==========================================================
# 呼叫 AI 做結構化整理
# ==========================================================
def _analyze(cfg: CategoryConfig, title: str, content: str):
    system_prompt = (
        "你是一位專業的台灣與全球財經/科技股分析師,也是精準的資料結構化助手。"
        "你只會回傳合法 JSON,不會多寫任何說明文字,也不會用 markdown 包起來。"
    )
    today = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    user_prompt = f"""請仔細閱讀以下內容,並嚴格依指定 JSON 結構回傳。

【今天日期】{today}(台灣時間)
【標題】{title}
【內文】{content}

任務:{cfg.task}

注意:處理時程(timelines)時,若新聞只寫月份/日期、沒寫年份,請以「今天日期」為基準推斷正確年份(通常是今年或最近的合理年份),不要預設成過去的年份。

請「只」回傳符合下列結構的 JSON,沒有資料的欄位給空陣列或空字串;陣列內一律放純文字字串(不要包成物件):
{cfg.schema_hint}
"""
    # 會抽概念的分類:附上標準清單(白名單),讓 AI 優先用統一名稱
    if cfg.to_concept_pairs:
        whitelist = _get_concept_whitelist()
        if whitelist:
            user_prompt += (
                "\n\n【概念/族群標準清單】抽取概念或族群標籤時,若意思與下列清單中的項目相同,"
                "請『直接沿用清單裡的標準寫法』;只有清單真的找不到對應時,才自行命名(用業界慣用簡稱)。\n"
                + "、".join(whitelist)
            )

    last_err = None
    for _ in range(2):
        try:
            completion = _ai_chat(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2000,
            )
            raw = completion.choices[0].message.content or ""
            return cfg.model.model_validate(_safe_json_loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = e
    raise RuntimeError(f"AI 回傳格式解析失敗(已重試):{last_err}")


def _safe_json_loads(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ==========================================================
# 小工具:清單字串切分
# ==========================================================
_LIST_SPLIT_RE = re.compile(r"[,、,;；/]")


def _split_list(s: str) -> List[str]:
    return [t.strip() for t in _LIST_SPLIT_RE.split(s) if t.strip()]


def _get_concept_whitelist() -> List[str]:
    """概念白名單 = 內建種子 ∪ Notion 概念族群庫現有概念。
    隨著新增資料到 Notion 自動成長(notion_timeline 會在新建概念時讓名稱快取失效),
    AI 標概念時用它統一名稱。不再讀 Google Sheet。"""
    names = list(_CONCEPT_SEED)
    try:
        if notion_timeline.enabled():
            names += notion_timeline.concept_names()
    except notion_timeline.NotionError as e:
        logger.warning("讀 Notion 概念清單失敗,改用內建種子:%s", e)
    out, seen = [], set()
    for n in names:
        n = (n or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ==========================================================
# 對外的一站式函數:給 Telegram/LINE 機器人呼叫
# ==========================================================
def route_and_store(text: str) -> Result:
    # 「說明」指令 → 回指令總表
    if _is_help(text):
        return Result("說明", _HELP_TEXT)

    # 先看是不是「查詢」(用「查」「問」等開頭),是的話走查詢、不寫入
    question = _detect_query(text)
    if question is not None:
        if not QUERY_ENABLED:
            return Result("查詢(暫停)", "🔍 查詢功能整理中、暫停使用(資料庫完善後會再開放)。")
        if not question:
            raise NoCategoryError(
                "🔍 查詢用法:在「查」後面接你的問題。\n例如:\n查 光聖最近有什麼時程\n查 這週的全球局勢重點"
            )
        return _answer_query(question)

    # 「刪隨筆/改隨筆」→ 刪除或修改今天的隨筆;要在通用「改」修正指令之前判斷
    # (否則「改隨筆」會被 _handle_correction 的「改」攔走)
    note_delete = _handle_note_delete(text)
    if note_delete is not None:
        return note_delete
    note_edit = _handle_note_edit(text)
    if note_edit is not None:
        return note_edit

    # 「刪關注/改關注」→ 刪或改明日關注;同樣要在通用「改」修正指令之前判斷
    watchlist_delete = _handle_watchlist_delete(text)
    if watchlist_delete is not None:
        return watchlist_delete
    watchlist_edit = _handle_watchlist_edit(text)
    if watchlist_edit is not None:
        return watchlist_edit

    # 再看是不是「修正指令」(主表 / 改),是的話處理、不寫入新聞
    correction = _handle_correction(text)
    if correction is not None:
        return correction

    # 「時程」開頭 → 寫進 Notion 時間軸(不走 Google Sheet)
    timeline = _handle_timeline(text)
    if timeline is not None:
        return timeline

    # 「今日關注/明日關注」→ 列出該日關注清單;要在 _handle_watchlist 之前判斷
    watchlist_list = _handle_watchlist_list(text)
    if watchlist_list is not None:
        return watchlist_list

    # 「關注」開頭 → 隔日盯盤筆記,寫進 Notion(隔天 08:30 隨推播帶出)
    watchlist = _handle_watchlist(text)
    if watchlist is not None:
        return watchlist

    # 「今日隨筆」→ 列出當天已記的隨筆(依時間排序);要在 _handle_note 之前判斷
    today_notes = _handle_today_notes(text)
    if today_notes is not None:
        return today_notes

    # 「隨筆」開頭 → 記一則隨手筆記,寫進 Notion(當天 21:00 隨推播彙整)
    note = _handle_note(text)
    if note is not None:
        return note

    # 「更新法說會/抓法說會」→ 立即抓 money-link 法說會寫進時程庫
    fetched = _handle_fetch_earnings(text)
    if fetched is not None:
        return fetched

    # 「更新經濟/抓經濟」→ 立即經 Firecrawl 抓 MacroMicro 美國經濟事件
    econ = _handle_fetch_econ(text)
    if econ is not None:
        return econ

    cfg, content = detect_category(text)
    if cfg is None:
        raise NoCategoryError(GUIDANCE)

    # 若內容含連結 → 自動抓全文(你只丟連結就好)
    url = _first_url(content)
    fetched_title = ""
    if url:
        try:
            fetched_title, article = fetch_article(url)
            user_note = _URL_RE.sub("", content).strip(" \n:：、,，。-")
            content = article
            if len(user_note) >= 4:
                content = f"{article}\n\n【讀者備註】{user_note}"
        except FetchError as e:
            logger.warning("抓取全文失敗,退回使用者貼的文字:%s", e)
            # 若使用者只丟了連結、沒有可分析的內文 → 明確提示
            if len(_URL_RE.sub("", content).strip()) < 12:
                raise NoCategoryError(
                    "⚠️ 這個連結抓不到內文(可能需要登入或被網站擋爬蟲)。\n"
                    "請直接複製新聞內文貼上,我就能幫你整理。"
                ) from e

    if len(content.strip()) < 12:
        raise NoCategoryError(f"已辨識分類『{cfg.label}』,但下面沒看到內容。請在關鍵字的下一行貼上要記錄的內容,或貼一個新聞連結。")

    if not notion_timeline.enabled():
        raise NoCategoryError(
            "⚠️ 尚未啟用 Notion:請先設定 NOTION_TOKEN,並把「股票投資大腦」頁面分享給該 integration。"
        )

    content = content[:MAX_CONTENT_CHARS]
    title = fetched_title or _extract_title(content)
    analysis = _analyze(cfg, title, content)
    now = _now_str()

    # 寫進 Notion(取代 Google Sheet);概念自動找/建並掛到個股
    try:
        _ok, addendum = cfg.to_notion(analysis, title, url, now)
    except notion_timeline.NotionError as e:
        logger.warning("寫入 Notion 失敗:%s", e)
        raise NoCategoryError("⚠️ 寫入 Notion 失敗,請稍後再試一次。") from e

    reply = cfg.format_reply(analysis)
    if addendum:
        reply = f"{reply}\n\n{addendum}"
    return Result(label=cfg.label, reply=reply)


def _first_url(text: str) -> str:
    m = _URL_RE.search(text)
    return m.group(0) if m else ""


def _extract_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("http"):
            return line[:120]
    return text.strip()[:120] or "(未命名)"


# ==========================================================
# 反向查詢:讀 Sheet 各分頁,交給 Claude 依資料回答
# ==========================================================
ALL_CONFIGS = [INDIVIDUAL, INDUSTRY, REPORT, GLOBAL, KNOWLEDGE]

# 查詢時每個分頁最多取的近期列數,與送進 AI 的總字元預算
QUERY_ROWS_PER_TAB = int(os.environ.get("QUERY_ROWS_PER_TAB", "80"))
QUERY_CHAR_BUDGET = int(os.environ.get("QUERY_CHAR_BUDGET", "15000"))


def _detect_query(text: str):
    """開頭是查詢前綴 → 回傳問題字串(可能為空);否則回傳 None。"""
    s = text.strip()
    first_line = s.splitlines()[0].strip() if s else ""
    for p in _QUERY_PREFIXES:
        if first_line.startswith(p):
            return s[len(p):].strip(" :：、,，.。?？\n")
    return None


_TIMELINE_USAGE = (
    "🗓️ 時程用法:「時程」後面用一句話描述,我會自動抓出個股、日期、事件並寫進 Notion 時間軸。\n"
    "例如:\n"
    "• 時程 8131福懋科 8月量產\n"
    "• 時程 2330 7/17 法說會\n"
    "• 時程 6442光聖 Q3 台北國際光電展\n"
    "• 時程 3583 擴廠 8月試產、11月量產\n"
    "\n"
    "批次(一次多檔):第一行只打「時程」,之後一行一筆:\n"
    "時程\n"
    "2330 台積電 法說會 7/16\n"
    "3008 大立光 法說會 7/9"
)


def _parse_timeline(body: str) -> TimelineInput:
    """用 AI 把一句話拆成結構化時程事件。"""
    today = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    system_prompt = (
        "你是精準的台股時程資料結構化助手。只回傳合法 JSON,不寫任何說明,不要用 markdown 包起來。"
    )
    user_prompt = f"""請把下面這句話拆成一筆「股票時程事件」,嚴格回傳指定 JSON。

【今天日期】{today}(台灣時間)
【輸入】{body}

規則:
- stock_code:台股代號(3-6 位數字),沒有就給空字串。
- stock_name:個股名稱(去掉代號),沒有就空字串。
- title:精簡事件標題(例如「Q2 法說會」「桃園三廠量產」「台北國際光電展」)。
- date_start / date_end:一律 YYYY-MM-DD。只寫月份(如「8月」)就用該月 1 日;只寫季(Q1~Q4)用該季首月 1 日;沒寫年份用「今天日期」推最近的合理年份(通常今年或明年,不要用過去)。單一時間點只填 date_start;有明確區間(如「8月試產、11月量產」)才填 date_end。
- event_type:必須從這個清單挑最接近的一個:擴廠進度、試產進度、量產進度、發行可轉債、CB掛牌、CB拆解、法說會、股東會、除權息、增減資、營收公布、財報公布、財報利空/多、展覽/政策、其他。
- note:原句裡的補充資訊(沒有就空字串)。

只回傳這個結構的 JSON:
{{"stock_code":"","stock_name":"","title":"","date_start":"","date_end":"","event_type":"","note":""}}
"""
    last_err = None
    for _ in range(2):
        try:
            completion = _ai_chat(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=600,
            )
            raw = completion.choices[0].message.content or ""
            return TimelineInput.model_validate(_safe_json_loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = e
    raise RuntimeError(f"AI 解析時程失敗(已重試):{last_err}")


def _parse_timeline_many(bodies: List[str]) -> List[TimelineInput]:
    """用一次 AI 呼叫把多行時程各拆成結構化事件,回傳與輸入等長、順序對應的清單。
    只有一行就走單筆解析;數量對不上或解析失敗時退回逐行解析,保證每行都有結果。"""
    if len(bodies) == 1:
        return [_parse_timeline(bodies[0])]

    today = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    numbered = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(bodies))
    system_prompt = (
        "你是精準的台股時程資料結構化助手。只回傳合法 JSON,不寫任何說明,不要用 markdown 包起來。"
    )
    user_prompt = f"""請把下面【每一行】各拆成一筆「股票時程事件」,依原順序回傳。

【今天日期】{today}(台灣時間)
【輸入(每行一筆,共 {len(bodies)} 筆)】
{numbered}

每一筆的規則:
- stock_code:台股代號(3-6 位數字),沒有就給空字串。
- stock_name:個股名稱(去掉代號),沒有就空字串。
- title:精簡事件標題(例如「Q2 法說會」「桃園三廠量產」「台北國際光電展」)。
- date_start / date_end:一律 YYYY-MM-DD。只寫月份(如「8月」)用該月 1 日;只寫季(Q1~Q4)用該季首月 1 日;沒寫年份用「今天日期」推最近的合理年份(通常今年或明年,不要用過去)。單一時間點只填 date_start;有明確區間(如「8月試產、11月量產」)才填 date_end。
- event_type:必須從這個清單挑最接近的一個:擴廠進度、試產進度、量產進度、發行可轉債、CB掛牌、CB拆解、法說會、股東會、除權息、增減資、營收公布、財報公布、財報利空/多、展覽/政策、其他。
- note:原句裡的補充資訊(沒有就空字串)。

嚴格回傳這個結構(events 陣列長度必須等於 {len(bodies)},順序對應每一行):
{{"events":[{{"stock_code":"","stock_name":"","title":"","date_start":"","date_end":"","event_type":"","note":""}}]}}
"""
    for _ in range(2):
        try:
            completion = _ai_chat(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=300 * len(bodies) + 300,
            )
            raw = completion.choices[0].message.content or ""
            obj = _safe_json_loads(raw)
            events = obj.get("events") if isinstance(obj, dict) else None
            if isinstance(events, list) and len(events) == len(bodies):
                return [TimelineInput.model_validate(e) for e in events]
        except (json.JSONDecodeError, ValidationError, KeyError, TypeError, AttributeError):
            pass
    # 一次拆多筆失敗 → 退回逐行解析(較慢但保證每行有結果)
    return [_parse_timeline(b) for b in bodies]


class _TimelineItemError(Exception):
    """單一筆時程解析/寫入的可回復錯誤(批次時記下該行、其餘照寫)。"""


def _store_timeline_event(data: TimelineInput, body: str) -> dict:
    """寫入一筆時程事件到 Notion。成功回傳含摘要欄位的 dict;
    可回復錯誤(看不出日期)丟 _TimelineItemError,由呼叫端決定要略過或回提示。"""
    if not data.title:
        data.title = body[:60]
    if not data.date_start:
        raise _TimelineItemError(
            f"看不出「{body}」裡的日期(請補上「8月量產」「7/17 法說會」「Q3」之類)"
        )

    # 找個股頁(用代號優先);找不到就自動新增到個股主表再關聯
    stock = None
    warn = ""
    auto_created = False
    is_cb_unfiled = False
    try:
        stock = notion_timeline.find_stock_page(data.stock_code, data.stock_name)
    except notion_timeline.NotionError as e:
        logger.warning("查個股頁失敗:%s", e)

    if stock is None and (data.stock_code or data.stock_name):
        auto_create = os.environ.get("AUTO_CREATE_STOCK", "1") != "0"
        is_cb = _is_non_stock_code(f"{data.stock_code} {data.stock_name}")
        if auto_create and not is_cb:
            try:
                stock = notion_timeline.create_stock_page(data.stock_code, data.stock_name)
                auto_created = True
            except notion_timeline.NotionError as e:
                logger.warning("自動新增個股失敗:%s", e)
        if stock is None and is_cb:
            is_cb_unfiled = True
            warn = (
                f"\nℹ️「{(data.stock_code + ' ' + data.stock_name).strip()}」為 5~6 碼(CB/非現股),"
                "已建事件但未建檔到個股主表(只有一般四碼現股才自動建檔)。"
            )
        elif stock is None:
            warn = (
                f"\n⚠️ 個股主表查無「{(data.stock_code + ' ' + data.stock_name).strip()}」,"
                "已建事件但未關聯(概念分類需要關聯個股)。可先在個股主表新增此股,再手動連結。"
            )

    if auto_created:
        warn = (
            f"\n🆕 個股主表原本沒有「{stock['label']}」,已自動新增並關聯。"
            "(概念分類請之後到該股補上「隸屬概念」,或用其他新聞餵入)"
        )

    # 事件標題一律帶上公司名,行事曆才看得出是哪家公司(標題已含名稱/代號就不重複加)
    company = (stock["label"] if stock else "") or data.stock_name or data.stock_code
    has_company = bool(
        (data.stock_name and data.stock_name in data.title)
        or (data.stock_code and data.stock_code in data.title)
    )
    event_title = data.title if (has_company or not company) else f"{company} {data.title}".strip()

    result = notion_timeline.add_event(
        title=event_title,
        date_start=data.date_start,
        date_end=data.date_end,
        event_type=data.event_type,
        stock_page_id=stock["id"] if stock else "",
        concept_ids=stock.get("concept_ids") if stock else None,
        note=data.note,
        source="LINE",
    )

    date_txt = data.date_start + (f" ~ {data.date_end}" if data.date_end else "")
    return {
        "event_title": event_title,
        "date_txt": date_txt,
        "event_type": data.event_type or "其他",
        "stock": stock,
        "note": data.note,
        "warn": warn,
        "auto_created": auto_created,
        "is_cb_unfiled": is_cb_unfiled,
        "url": result.get("url", ""),
    }


def _handle_timeline(text: str):
    """開頭是「時程」前綴 → 寫進 Notion 時間軸並回覆;支援批次(一行一筆);否則回傳 None。"""
    s = text.strip()
    lines = s.splitlines()
    first_line = lines[0].strip() if lines else ""
    matched = next((p for p in _TIMELINE_PREFIXES if first_line.startswith(p)), None)
    if matched is None:
        return None

    # 收集「事件行」:前綴同一行若有內容算一筆,之後每個非空行各算一筆(可一次多檔)
    bodies: List[str] = []
    head = first_line[len(matched):].strip(" :：、,，.。")
    if head:
        bodies.append(head)
    for ln in lines[1:]:
        ln = ln.strip(" :：、,，.。")
        if ln:
            bodies.append(ln)

    if not bodies:
        raise NoCategoryError(_TIMELINE_USAGE)

    # 一次太多筆會拖慢寫入、可能超過 LINE 回覆時限,請分批
    if len(bodies) > 20:
        raise NoCategoryError(
            f"🗓️ 一次最多 20 筆(這次 {len(bodies)} 筆)。請分次貼,避免寫入逾時。"
        )

    if not notion_timeline.enabled():
        raise NoCategoryError(
            "⚠️ 時程功能尚未啟用:請先在環境變數設定 NOTION_TOKEN"
            "(Notion integration 密鑰,並把「股票投資大腦」頁面分享給該 integration)。"
        )

    parsed = _parse_timeline_many(bodies)

    # ---- 單筆:維持原本詳細回覆 ----
    if len(bodies) == 1:
        try:
            r = _store_timeline_event(parsed[0], bodies[0])
        except _TimelineItemError as e:
            raise NoCategoryError(f"🗓️ {e}")
        stock = r["stock"]
        linked = f"🔗 {stock['label']}" if stock else "(未關聯個股)"
        n_concept = len(stock.get("concept_ids") or []) if stock else 0
        concept_txt = f"（已自動歸類 {n_concept} 個概念）" if n_concept else ""
        reply = (
            "🗓️ 已寫入 Notion 時間軸\n"
            f"• 事件:{r['event_title']}\n"
            f"• 日期:{r['date_txt']}\n"
            f"• 類型:{r['event_type']}\n"
            f"• 個股:{linked}{concept_txt}"
            + (f"\n• 備註:{r['note']}" if r["note"] else "")
            + r["warn"]
        )
        if r["url"]:
            reply += f"\n{r['url']}"
        return Result(label="時程", reply=reply)

    # ---- 批次:每行一筆,逐筆寫入,回覆彙整(單筆失敗不影響其他筆) ----
    ok_lines, fail_lines = [], []
    for data, body in zip(parsed, bodies):
        try:
            r = _store_timeline_event(data, body)
        except _TimelineItemError as e:
            fail_lines.append(f"⚠️ {body} — {e}")
            continue
        except Exception as e:  # 單筆寫入意外失敗也不中斷整批
            logger.warning("批次時程單筆寫入失敗:%s", e)
            fail_lines.append(f"⚠️ {body} — 寫入失敗:{e}")
            continue
        if r["stock"]:
            mark = "🆕" if r["auto_created"] else "🔗"
        else:
            mark = "·CB未建檔" if r["is_cb_unfiled"] else "(未關聯)"
        ok_lines.append(f"✅ {r['event_title']} — {r['date_txt']} {mark}")

    head_txt = f"🗓️ 時程批次寫入:成功 {len(ok_lines)} 筆" + (
        f"、失敗 {len(fail_lines)} 筆" if fail_lines else ""
    )
    parts = [head_txt]
    if ok_lines:
        parts.append("\n".join(ok_lines))
    if fail_lines:
        parts.append("\n".join(fail_lines))
    return Result(label="時程", reply="\n\n".join(parts))


# 關注筆記的日期覆寫詞:詞 → (相對天數)
_WATCHLIST_DAY_WORDS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1}


def _handle_watchlist(text: str):
    """開頭是「關注」→ 把每一行當一筆盯盤筆記寫進 Notion(事件類型「每日關注」),
    預設日期為隔天,隔天 08:30 會隨群組推播帶出;否則回傳 None。"""
    s = text.strip()
    lines = [ln.strip() for ln in s.splitlines()]
    first_line = lines[0] if lines else ""
    matched = next((p for p in _WATCHLIST_PREFIXES if first_line.startswith(p)), None)
    if matched is None:
        return None

    if not notion_timeline.enabled():
        raise NoCategoryError(
            "⚠️ 關注功能尚未啟用:請先在環境變數設定 NOTION_TOKEN。"
        )

    # 第一行關鍵字後面剩下的文字(可能是第一筆,也可能只是日期詞)
    rest = first_line[len(matched):].strip(" :：、,，.。\n")

    # 日期覆寫:開頭若是 今天/今日/明天/明日,吃掉該詞;預設隔天
    offset = 1
    label_day = "明日"
    for word, off in _WATCHLIST_DAY_WORDS.items():
        if rest.startswith(word):
            offset = off
            label_day = "今日" if off == 0 else "明日"
            rest = rest[len(word):].strip(" :：、,，.。\n")
            break

    # 收集項目:第一行剩餘內容(若有)+ 後續每一非空行
    items = [rest] if rest else []
    items += [ln for ln in lines[1:] if ln]
    if not items:
        raise NoCategoryError(_WATCHLIST_USAGE)

    target = datetime.datetime.now(notion_timeline.TW_TZ).date() + datetime.timedelta(days=offset)
    date_iso = target.isoformat()

    # 採最新版:先清掉同一天的舊「每日關注」筆記,再寫入這次的
    replaced = 0
    try:
        replaced = notion_timeline.archive_events_by_type_date(_WATCHLIST_TYPE, date_iso)
    except notion_timeline.NotionError as e:
        logger.warning("關注:清除舊筆記失敗:%s", e)

    added = []
    for item in items:
        # 盡量關聯個股(用代號優先);關聯不到就只記事件、不自動新增個股(避免主表被盯盤筆記灌爆)
        stock = None
        try:
            stock = notion_timeline.find_stock_page("", item)
        except notion_timeline.NotionError as e:
            logger.warning("關注:查個股頁失敗:%s", e)
        try:
            notion_timeline.add_event(
                title=item[:200],
                date_start=date_iso,
                event_type=_WATCHLIST_TYPE,
                stock_page_id=stock["id"] if stock else "",
                concept_ids=stock.get("concept_ids") if stock else None,
                source="LINE",
                status="預定",
            )
            added.append((item, bool(stock)))
        except notion_timeline.NotionError as e:
            logger.warning("關注:寫入失敗(%s):%s", item, e)
            added.append((item + "  ⚠️寫入失敗", False))

    m, d = target.month, target.day
    ok = [a for a in added if not a[0].endswith("寫入失敗")]
    body = "\n".join(
        f"• {name} {'🔗' if linked else ''}".rstrip() for name, linked in added
    )
    tail = f"(共 {len(ok)} 筆" + (f",已覆蓋前一版 {replaced} 筆)" if replaced else ")")
    reply = (
        f"👀 已更新{label_day}({m}/{d})關注,{label_day}早上 08:30 會隨推播通知一次:\n"
        f"{body}\n"
        f"{tail}"
    )
    return Result(label="關注", reply=reply)


def _watchlist_items(offset: int):
    """回 (target_date, [{'id','title'}]):某日(offset 天後)的每日關注清單。"""
    d = datetime.datetime.now(notion_timeline.TW_TZ).date() + datetime.timedelta(days=offset)
    return d, notion_timeline.list_events_by_type_date(_WATCHLIST_TYPE, d.isoformat())


def _handle_watchlist_list(text: str):
    """整段是「今日關注/明日關注」→ 列出該日關注清單(有編號);否則 None。"""
    s = text.strip()
    if s in _WATCHLIST_TMR_WORDS:
        offset, label = 1, "明日"
    elif s in _WATCHLIST_TODAY_WORDS:
        offset, label = 0, "今日"
    else:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    d, items = _watchlist_items(offset)
    head = f"👀 {label}關注 {d.month}/{d.day}"
    if not items:
        hint = ",用「關注 …」開始盯" if offset == 1 else ""
        return Result(label="關注清單", reply=f"{head}\n\n({label}還沒有關注{hint})")
    body = "\n".join(f"{i}. {it['title']}" for i, it in enumerate(items, 1))
    tip = "\n\n改/刪打「刪關注 編號」或「改關注 編號 新內容」(針對明日)" if offset == 1 else ""
    return Result(label="關注清單", reply=f"{head}(共 {len(items)} 筆)\n\n{body}{tip}")


def _handle_watchlist_delete(text: str):
    """「刪關注 [編號…]」→ 刪掉明日關注第 N 筆;不帶編號=刪最新一筆。"""
    m = _match_prefix(text, _WATCHLIST_DELETE_PREFIXES)
    if m is None:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    _, rest = m
    _, items = _watchlist_items(1)
    if not items:
        return Result(label="刪關注", reply="明日還沒有關注可刪。")
    tokens = rest.split()
    if not tokens:
        idxs = [len(items)]  # 不帶編號 → 刪最新(列表最後一筆)
    else:
        try:
            idxs = sorted({int(t) for t in tokens})
        except ValueError:
            raise NoCategoryError(
                "⚠️ 編號要用數字,例如「刪關注 2」或「刪關注 2 4」。\n先打「明日關注」看編號。"
            )
    bad = [i for i in idxs if i < 1 or i > len(items)]
    if bad:
        raise NoCategoryError(
            f"⚠️ 明日只有 {len(items)} 筆關注,沒有第 {'、'.join(map(str, bad))} 筆。\n"
            "先打「明日關注」看編號。"
        )
    deleted = []
    for i in idxs:  # 先由快照解析頁 id 再刪,避免刪一筆後編號位移
        it = items[i - 1]
        notion_timeline.archive_page(it["id"])
        deleted.append(f"{i}. {it['title']}")
    return Result(label="刪關注", reply=f"🗑️ 已從明日關注刪除 {len(deleted)} 筆:\n" + "\n".join(deleted))


def _handle_watchlist_edit(text: str):
    """「改關注 編號 新內容」→ 改明日關注第 N 筆內容。"""
    m = _match_prefix(text, _WATCHLIST_EDIT_PREFIXES)
    if m is None:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    _, rest = m
    parts = rest.split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        raise NoCategoryError(
            "⚠️ 用法:「改關注 編號 新內容」,例如「改關注 2 2330 台積電 站上季線」。\n"
            "先打「明日關注」看編號。"
        )
    idx, new_content = int(parts[0]), parts[1].strip()
    if not new_content:
        raise NoCategoryError("⚠️ 新內容不能空白。")
    _, items = _watchlist_items(1)
    if not items:
        return Result(label="改關注", reply="明日還沒有關注可改。")
    if idx < 1 or idx > len(items):
        raise NoCategoryError(
            f"⚠️ 明日只有 {len(items)} 筆關注,沒有第 {idx} 筆。先打「明日關注」看編號。"
        )
    old = items[idx - 1]
    notion_timeline.update_event_title(old["id"], new_content)
    preview = new_content if len(new_content) <= 60 else new_content[:60] + "…"
    return Result(
        label="改關注",
        reply=f"✏️ 明日關注第 {idx} 筆已改為:\n{preview}\n(原:{old['title'][:40]})",
    )


def _handle_note(text: str):
    """開頭是「隨筆/隨手筆記/隨手記/備忘」→ 記一則隨手筆記(當天日期)寫進 Notion;否則 None。"""
    s = text.strip()
    lines = s.splitlines()
    first = lines[0].strip() if lines else ""
    matched = next((p for p in _NOTE_PREFIXES if first.startswith(p)), None)
    if matched is None:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 隨筆功能尚未啟用:請先設定 NOTION_TOKEN。")

    # 內容 = 第一行去掉前綴後的剩餘 + 後續各行(保留換行)
    head = first[len(matched):].strip(" :：、,，.。\n")
    rest = "\n".join(lines[1:]).strip()
    content = (head + ("\n" + rest if rest else "")).strip() if head else rest
    if not content:
        raise NoCategoryError(_NOTE_USAGE)

    # Quick Note 首行存記錄當下的完整 ISO 時間當排序鍵(Notion 時間欄位只有分鐘精度,
    # 無法秒級排序);若內容超過標題長度,接在時間戳後面保存完整內容。
    now = datetime.datetime.now(notion_timeline.TW_TZ)
    overflow = content if len(content) > 200 else ""
    quick = now.isoformat(timespec="seconds") + (("\n" + overflow) if overflow else "")
    notion_timeline.add_event(
        title=content[:200],
        date_start=now.date().isoformat(),
        event_type=_NOTE_TYPE,
        note=quick,
        source="LINE",
        status="已發生",
    )
    preview = content if len(content) <= 60 else content[:60] + "…"
    return Result(
        label="隨筆",
        reply=f"📝 已記錄隨筆:\n{preview}\n\n今晚 21:00 會彙整今天的隨筆一起推播;打「今日隨筆」可即時查看。",
    )


def _handle_today_notes(text: str):
    """整段訊息是「今日隨筆」等 → 列出當天已記的隨筆(依時間排序);否則 None。"""
    if text.strip() not in _TODAY_NOTES_WORDS:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    today = datetime.datetime.now(notion_timeline.TW_TZ).date()
    notes = notion_timeline.list_notes_by_date(today.isoformat(), _NOTE_TYPE)
    head = f"📝 今日隨筆 {today.month}/{today.day}"
    if not notes:
        return Result(label="今日隨筆", reply=f"{head}\n\n(今天還沒有隨筆,用「隨筆 …」開始記)")
    body = "\n".join(
        f"{i}. {n['time'] + ' ' if n['time'] else ''}{n['title']}"
        for i, n in enumerate(notes, 1)
    )
    tip = "\n\n改/刪打「刪隨筆 編號」或「改隨筆 編號 新內容」"
    return Result(label="今日隨筆", reply=f"{head}(共 {len(notes)} 則)\n\n{body}{tip}")


def _match_prefix(text: str, prefixes) -> tuple[str, str] | None:
    """第一行以任一 prefix 開頭 → 回傳 (matched_prefix, 去掉前綴後整段訊息的剩餘文字)。"""
    s = text.strip()
    first = s.splitlines()[0].strip() if s else ""
    matched = next((p for p in prefixes if first.startswith(p)), None)
    if matched is None:
        return None
    rest = s[s.find(first) + len(matched):].strip(" :：、,，.。\n")
    return matched, rest


def _handle_note_delete(text: str):
    """「刪隨筆 [編號…]」→ 刪掉今天第 N 則隨筆(用「今日隨筆」的編號);不帶編號=刪最新一則。"""
    m = _match_prefix(text, _NOTE_DELETE_PREFIXES)
    if m is None:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    _, rest = m
    today = datetime.datetime.now(notion_timeline.TW_TZ).date()
    notes = notion_timeline.list_notes_by_date(today.isoformat(), _NOTE_TYPE)
    if not notes:
        return Result(label="刪隨筆", reply="今天還沒有隨筆可刪。")

    tokens = rest.split()
    if not tokens:
        idxs = [len(notes)]  # 不帶編號 → 刪最新一則(列表最後一筆)
    else:
        try:
            idxs = sorted({int(t) for t in tokens})
        except ValueError:
            raise NoCategoryError(
                "⚠️ 編號要用數字,例如「刪隨筆 2」或「刪隨筆 2 4」。\n先打「今日隨筆」看編號。"
            )
    bad = [i for i in idxs if i < 1 or i > len(notes)]
    if bad:
        raise NoCategoryError(
            f"⚠️ 今天只有 {len(notes)} 則隨筆,沒有第 {'、'.join(map(str, bad))} 則。\n"
            "先打「今日隨筆」看編號。"
        )
    # 先由快照解析出頁 id 再刪,避免刪一筆後編號位移
    deleted = []
    for i in idxs:
        n = notes[i - 1]
        notion_timeline.archive_page(n["id"])
        deleted.append(f"{i}. {n['title']}")
    body = "\n".join(deleted)
    return Result(label="刪隨筆", reply=f"🗑️ 已刪除 {len(deleted)} 則隨筆:\n{body}")


def _handle_note_edit(text: str):
    """「改隨筆 編號 新內容」→ 把今天第 N 則隨筆改成新內容(保留原記錄時間、維持排序位置)。"""
    m = _match_prefix(text, _NOTE_EDIT_PREFIXES)
    if m is None:
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    _, rest = m
    parts = rest.split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        raise NoCategoryError(
            "⚠️ 用法:「改隨筆 編號 新內容」,例如「改隨筆 2 台積電擴產留意設備股」。\n"
            "先打「今日隨筆」看編號。"
        )
    idx, new_content = int(parts[0]), parts[1].strip()
    if not new_content:
        raise NoCategoryError("⚠️ 新內容不能空白。")
    today = datetime.datetime.now(notion_timeline.TW_TZ).date()
    notes = notion_timeline.list_notes_by_date(today.isoformat(), _NOTE_TYPE)
    if not notes:
        return Result(label="改隨筆", reply="今天還沒有隨筆可改。")
    if idx < 1 or idx > len(notes):
        raise NoCategoryError(
            f"⚠️ 今天只有 {len(notes)} 則隨筆,沒有第 {idx} 則。先打「今日隨筆」看編號。"
        )
    old = notes[idx - 1]
    notion_timeline.update_note(old["id"], new_content)
    preview = new_content if len(new_content) <= 60 else new_content[:60] + "…"
    return Result(
        label="改隨筆",
        reply=f"✏️ 第 {idx} 則已改為:\n{preview}\n(原:{old['title'][:40]})",
    )


_FETCH_EARNINGS_PREFIXES = ("更新法說會", "抓法說會", "法說會更新")


def _handle_fetch_earnings(text: str):
    """開頭是「更新法說會/抓法說會」→ 立即抓 money-link 法說會寫進時程庫;否則回傳 None。"""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not any(first.startswith(p) for p in _FETCH_EARNINGS_PREFIXES):
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")

    import fetchers
    res = fetchers.sync_earnings_calls()
    lines = [
        "📡 法說會自動更新完成(來源:money-link)",
        f"比對追蹤股 {res['matched']} 檔 → 新增 {res['added']} 筆、已存在略過 {res['skipped']} 筆",
    ]
    if res["added_items"]:
        lines.append("")
        lines += [f"• {it}" for it in res["added_items"][:60]]
    elif res["added"] == 0:
        lines.append("(追蹤股近期法說會都已在時程庫,無新增)")
    return Result(label="法說會更新", reply="\n".join(lines))


_FETCH_ECON_PREFIXES = ("更新經濟", "抓經濟", "更新經濟數據", "經濟數據更新")


def _handle_fetch_econ(text: str):
    """開頭是「更新經濟/抓經濟」→ 立即經 Firecrawl 抓 MacroMicro 美國經濟事件寫進時程庫;否則 None。"""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not any(first.startswith(p) for p in _FETCH_ECON_PREFIXES):
        return None
    if not notion_timeline.enabled():
        raise NoCategoryError("⚠️ 尚未啟用:請先設定 NOTION_TOKEN。")
    import fetchers
    if not fetchers.econ_enabled():
        raise NoCategoryError("⚠️ 美國經濟事件功能未啟用:請先在環境變數設定 FIRECRAWL_API_KEY。")
    res = fetchers.sync_econ_events()
    lines = [
        "🏦 美國經濟事件更新完成(來源:MacroMicro)",
        f"抓到未來美國事件 {res['found']} 筆 → 新增 {res['added']}、已存在略過 {res['skipped']}",
    ]
    if res["added_items"]:
        lines.append("")
        lines += [f"• {it}" for it in res["added_items"][:60]]
    elif res["added"] == 0:
        lines.append("(近期美國經濟事件都已在時程庫,無新增)")
    return Result(label="經濟數據更新", reply="\n".join(lines))


def _gather_corpus() -> str:
    """從 Notion 各庫撈近期資料攤平成精簡文字(較新的在前),供 AI 檢索。"""
    nt = notion_timeline
    if not nt.enabled():
        return ""
    blocks: List[str] = []

    # 個股 ↔ 概念 對照,並建 id→名稱 對照表(供 relation 欄位換名)
    id_name: dict = {}
    try:
        stocks = nt.list_stocks()
        concepts = nt.list_concepts()
        id_name = {s["id"]: s["label"] for s in stocks}
        id_name.update({c["id"]: c["name"] for c in concepts})
        for s in stocks:
            cnames = [id_name.get(cid, "") for cid in s.get("concept_ids", [])]
            cnames = [c for c in cnames if c]
            if cnames:
                blocks.append(f"〔個股概念〕{s['label']}:{', '.join(cnames)}")
    except nt.NotionError as e:
        logger.warning("讀個股/概念對照失敗:%s", e)

    # 新聞與時程動態庫 / 全球局勢 / 知識
    for ds_id, tag, use_map in (
        (nt.TIMELINE_DS, "新聞/時程", True),
        (nt.GLOBAL_DS, "全球局勢", False),
        (nt.KNOWLEDGE_DS, "知識", False),
    ):
        try:
            for pg in nt.recent_pages(ds_id, QUERY_ROWS_PER_TAB):
                txt = nt.page_to_text(pg, id_name if use_map else None)
                if txt:
                    blocks.append(f"〔{tag}〕" + txt)
        except nt.NotionError as e:
            logger.warning("讀 %s 失敗:%s", tag, e)

    return "\n".join(blocks)[:QUERY_CHAR_BUDGET]


def _answer_query(question: str) -> Result:
    corpus = _gather_corpus()
    if not corpus.strip():
        return Result(label="查詢", reply="📭 資料庫目前還沒有任何記錄可以查詢。先貼幾則新聞給我吧!")

    today = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    system_prompt = (
        "你是使用者私人的財經筆記助理。只根據下方提供的『資料庫內容』回答問題,"
        "絕對不要編造資料庫裡沒有的事實。若找不到相關記錄,就明白說目前沒有相關記錄。"
        "用繁體中文、精簡條列回答,適時標出日期與新聞標題,讓使用者能回去查原文。"
    )
    user_prompt = (
        f"今天是 {today}(台灣時間)。\n\n"
        f"以下是使用者 Notion 投資筆記資料庫(較新的在前):\n\n{corpus}\n\n"
        f"---\n使用者的問題:{question}\n\n請只根據上面的資料庫內容回答。"
    )
    completion = _ai_chat(
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1200,
    )
    answer = (completion.choices[0].message.content or "").strip() or "(沒有得到回應,請再試一次)"
    return Result(label="查詢", reply=f"🔍 {question}\n\n{answer}"[:4500])


# ==========================================================
# 修正指令:改概念股主表 / 改某一筆新聞
# ==========================================================
_MASTER_HELP = (
    "🛠️ 概念股主表指令:\n"
    "• 主表 加股 <代號 名稱>       例:主表 加股 3529 力旺(只建檔,不掛概念)\n"
    "• 主表 加 <概念>:<個股>       例:主表 加 CPO:6442 光聖\n"
    "• 主表 移除 <概念>:<個股>     例:主表 移除 光通訊:2330\n"
    "• 主表 移除股 <個股>          例:主表 移除股 8111(從所有概念移除)\n"
    "• 主表 改股 <舊>:<新>         例:主表 改股 8111:6442 光聖(股號打錯一次改掉)\n"
    "• 主表 合併 <概念A>:<概念B>   例:主表 合併 共同封裝光學:CPO\n"
    "• 主表 刪除 <概念>            例:主表 刪除 半導體"
)
_EDIT_HELP = (
    "🛠️ 修改某一筆新聞(Notion)指令:\n"
    "改 <分類> <關鍵字> <欄位>:<新值>\n"
    "例:改 個股新聞 光聖 日期:2026-08-15\n"
    "可改欄位:標題、內容(摘要/備註)、日期、連結、狀態、類型\n"
    "(找該分類最近一筆含關鍵字的記錄來改;產業報告的目標價/券商等細項收在「內容」裡,"
    "請整段改內容或直接到 Notion 編輯)"
)


def _split_colon(s: str):
    """用第一個半形或全形冒號切成 (左, 右)。"""
    idx = min([i for i in (s.find(":"), s.find("：")) if i != -1], default=-1)
    if idx == -1:
        return s.strip(), ""
    return s[:idx].strip(), s[idx + 1:].strip()


def _cfg_by_word(word: str):
    """把分類詞(產業報告/個股/…)對應到 CategoryConfig(依關鍵字由長到短)。"""
    w = word.strip()
    for kw, cfg in _KEYWORDS:
        if w.startswith(kw):
            return cfg
    return None


def _handle_correction(text: str):
    """若是修正指令就處理並回 Result;否則回 None。"""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if first.startswith("主表"):
        return _master_command(text)
    # 全面改股:把舊股頁併到新股頁(概念與新聞關聯一起搬)— 需在通用「改」之前判斷
    for p in ("改股", "換股", "全部改股"):
        if first.startswith(p):
            old, new = _split_colon(first[len(p):])
            if not old or not new:
                return Result("改股", "用法:改股 <舊>:<新>(把舊股併到新股)\n例:改股 8111:6442 光聖")
            return _merge_stock(old, new)
    for p in ("修改", "改"):
        if first.startswith(p):
            return _edit_row_command(text, p)
    return None


def _merge_stock(old: str, new: str) -> Result:
    """把打錯的股 old 併到正確股 new(Notion):把 old 的隸屬概念與相關新聞/時程都改掛到 new,再封存 old 頁。"""
    nt = notion_timeline
    if not nt.enabled():
        return Result("改股", "⚠️ 尚未啟用 Notion:請先設定 NOTION_TOKEN。")
    try:
        oldpage = nt.find_stock_page("", old)
    except nt.NotionError as e:
        return Result("改股", f"⚠️ 查詢失敗:{str(e)[:150]}")
    if not oldpage:
        return Result("改股", f"⚠️ 個股主表找不到「{old}」")
    try:
        newpage = nt.find_stock_page("", new) or nt.create_stock_page("", new)
    except nt.NotionError as e:
        return Result("改股", f"⚠️ 建/找新股失敗:{str(e)[:150]}")
    if oldpage["id"] == newpage["id"]:
        return Result("改股", f"⚠️「{old}」與「{new}」指到同一檔,未變更")

    moved_concepts = moved_news = 0
    try:
        old_concepts = nt.get_stock_concepts(oldpage["id"])
        if old_concepts:
            nt.link_stock_concepts(newpage["id"], old_concepts)
            moved_concepts = len(old_concepts)
        for nid in nt.stock_news_ids(oldpage["id"]):
            try:
                nt.replace_event_stock(nid, oldpage["id"], newpage["id"])
                moved_news += 1
            except nt.NotionError as e:
                logger.warning("移轉新聞關聯失敗:%s", e)
        nt.archive_page(oldpage["id"])
    except nt.NotionError as e:
        return Result("改股", f"⚠️ 併股途中失敗(可能已部分完成):{str(e)[:150]}")

    return Result("改股", f"✅ 已把「{oldpage['label']}」併到「{newpage['label']}」並封存舊頁\n"
                          f"• 移轉概念 {moved_concepts} 個、新聞/時程 {moved_news} 筆")


def _master_command(text: str) -> Result:
    """概念↔個股修正(直接改 Notion 個股主表的「隸屬概念」與概念族群庫)。"""
    body = text.strip()[len("主表"):].strip()
    if not body:
        return Result("主表", _MASTER_HELP)
    parts = body.split(None, 1)
    action = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    nt = notion_timeline
    if not nt.enabled():
        return Result("主表", "⚠️ 尚未啟用 Notion:請先設定 NOTION_TOKEN。")

    try:
        if action in ("加", "新增", "加入"):
            concept, stocklist = _split_colon(rest)
            stocks = _split_list(stocklist)
            if not concept or not stocks:
                return Result("主表", "用法:主表 加 <概念>:<個股>\n例:主表 加 CPO:6442 光聖")
            cid = nt.find_or_create_concept(concept)
            linked, created = [], []
            for s in stocks:
                sp = nt.find_stock_page("", s)
                if not sp:
                    sp = nt.create_stock_page("", s)
                    created.append(sp["label"])
                nt.link_stock_concepts(sp["id"], [cid])
                linked.append(sp["label"])
            msg = f"✅ 已把概念【{concept}】掛到:{', '.join(linked)}"
            if created:
                msg += f"\n🆕 順便新增個股:{', '.join(created)}"
            return Result("主表", msg)

        if action in ("加股", "新增股", "加個股"):
            # 只建檔:不掛概念、不建事件。用於「先納入追蹤,概念之後再說」。
            # 進了主表就自動接上每 2 小時的重訊掃描與 EPS 抓取(兩者都是每輪重讀主表)。
            stocks = _split_list(rest)
            if not stocks:
                return Result("主表", "用法:主表 加股 <代號 名稱>\n"
                                      "例:主表 加股 3529 力旺\n"
                                      "(多檔用逗號分隔;只建檔不掛概念,概念用「主表 加 概念:個股」)")
            created, existed = [], []
            for s in stocks:
                sp = nt.find_stock_page("", s)
                if sp:
                    existed.append(sp["label"])
                else:
                    created.append(nt.create_stock_page("", s)["label"])
            lines = []
            if created:
                lines.append(f"✅ 已新增到個股主表:{', '.join(created)}")
                lines.append("• 下一輪重訊掃描(每 2 小時)起自動涵蓋")
                first = re.search(r"\d{3,6}", created[0])
                lines.append(f"• EPS 歷史:打「EPS 回補 {first.group(0) if first else '代號'}」立刻補,"
                             "或等週日 22:50 自動補")
            if existed:
                lines.append(f"ℹ️ 本來就在主表:{', '.join(existed)}")
            lines.append("⚠️ 確認一下代號有沒有打錯(代號錯了資料會抓成別家公司)")
            return Result("主表", "\n".join(lines))

        if action in ("移除", "刪股", "移除個股"):
            concept, stock = _split_colon(rest)
            if not concept or not stock:
                return Result("主表", "用法:主表 移除 <概念>:<個股>\n例:主表 移除 光通訊:2330")
            c = nt.find_concept(concept)
            if not c:
                return Result("主表", f"⚠️ 找不到概念【{concept}】")
            sp = nt.find_stock_page("", stock)
            if not sp:
                return Result("主表", f"⚠️ 找不到個股「{stock}」")
            if not nt.unlink_stock_concept(sp["id"], c["id"]):
                return Result("主表", f"⚠️「{sp['label']}」原本就沒有概念【{c['name']}】")
            return Result("主表", f"✅ 已把「{sp['label']}」從概念【{c['name']}】移除")

        if action in ("移除股", "全移除"):
            stock = rest.strip()
            if not stock:
                return Result("主表", "用法:主表 移除股 <個股>\n例:主表 移除股 8111")
            sp = nt.find_stock_page("", stock)
            if not sp:
                return Result("主表", f"⚠️ 找不到個股「{stock}」")
            n = len(nt.get_stock_concepts(sp["id"]))
            if n == 0:
                return Result("主表", f"⚠️「{sp['label']}」原本就沒有任何隸屬概念")
            nt.set_stock_concepts(sp["id"], [])
            return Result("主表", f"✅ 已清掉「{sp['label']}」的所有隸屬概念(原 {n} 個)")

        if action in ("改股", "換股", "更正股", "改代號"):
            old, new = _split_colon(rest)
            if not old or not new:
                return Result("主表", "用法:主表 改股 <舊>:<新>\n例:主表 改股 8111:6442 光聖")
            return _merge_stock(old, new)

        if action in ("合併", "併入", "改名"):
            a, b = _split_colon(rest)
            if not a or not b:
                return Result("主表", "用法:主表 合併 <概念A>:<概念B>(A 併入 B)\n例:主表 合併 共同封裝光學:CPO")
            ca = nt.find_concept(a)
            if not ca:
                return Result("主表", f"⚠️ 找不到來源概念【{a}】")
            cb = nt.find_concept(b)
            if not cb:
                nt.rename_concept(ca["id"], b)
                return Result("主表", f"✅ 已把概念【{a}】改名為【{b}】")
            members = nt.concept_member_ids(ca["id"])
            for sid in members:
                try:
                    nt.link_stock_concepts(sid, [cb["id"]])
                except nt.NotionError as e:
                    logger.warning("合併掛概念失敗:%s", e)
            nt.archive_page(ca["id"])
            return Result("主表", f"✅ 已把【{a}】({len(members)}檔)併入【{cb['name']}】並封存來源概念")

        if action in ("刪除", "刪", "刪概念"):
            concept = rest.strip()
            c = nt.find_concept(concept)
            if not c:
                return Result("主表", f"⚠️ 找不到概念【{concept}】")
            n = len(nt.concept_member_ids(c["id"]))
            nt.archive_page(c["id"])
            return Result("主表", f"✅ 已刪除(封存)概念【{c['name']}】(原 {n} 檔;各股的隸屬概念會自動移除)")
    except nt.NotionError as e:
        return Result("主表", f"⚠️ Notion 操作失敗:{str(e)[:180]}")

    return Result("主表", f"❓ 不認得的動作「{action}」。\n\n{_MASTER_HELP}")


# 欄位詞 → (Notion 屬性, 型別);依分類群組
_EDIT_FIELD_MAP = {
    "timeline": [  # 個股新聞 / 產業新聞 / 個股產業報告(都在新聞與時程動態庫)
        (("標題", "題目", "名稱"), "Name", "title"),
        (("摘要", "內容", "備註", "筆記", "總結", "重點"), "Quick Note", "rich_text"),
        (("日期", "時間"), "關鍵日期", "date"),
        (("連結", "網址", "來源"), "來源連結", "url"),
        (("狀態",), "狀態", "select"),
        (("類型",), "事件類型", "select"),
    ],
    "global": [
        (("標題", "題目", "名稱"), "Name", "title"),
        (("摘要", "內容"), "AI 摘要", "rich_text"),
        (("主題",), "影響主題", "multi_select"),
        (("市場", "資產"), "受影響市場資產", "multi_select"),
        (("日期", "時間"), "關鍵日期", "date"),
        (("連結", "網址", "來源"), "來源連結", "url"),
        (("狀態",), "狀態", "select"),
    ],
    "knowledge": [
        (("標題", "主題", "題目", "名稱"), "Name", "title"),
        (("重點", "內容", "整理"), "重點整理", "rich_text"),
        (("關鍵字", "標籤"), "關鍵字", "multi_select"),
        (("連結", "網址", "來源"), "來源連結", "url"),
    ],
}


def _resolve_edit_field(field_word: str, group: str):
    for words, prop, kind in _EDIT_FIELD_MAP[group]:
        if any(w in field_word for w in words):
            return prop, kind
    return None, None


def _build_edit_prop(kind: str, value: str):
    """回傳 (Notion 屬性值 或 None, 錯誤訊息)。"""
    if kind == "title":
        return {"title": [{"text": {"content": value[:200]}}]}, ""
    if kind == "rich_text":
        return {"rich_text": [{"text": {"content": value[:1900]}}]}, ""
    if kind == "url":
        return {"url": value}, ""
    if kind == "multi_select":
        return {"multi_select": notion_timeline._multi_select(_split_list(value))}, ""
    if kind == "date":
        iso = _valid_iso(value)
        if not iso:
            return None, "日期格式需 YYYY-MM-DD,例如 2026-08-15"
        return {"date": {"start": iso}}, ""
    if kind == "select":
        return {"select": {"name": value}}, ""
    return None, "不支援的欄位型別"


def _edit_row_command(text: str, prefix: str) -> Result:
    """修改某一筆新聞的欄位(Notion)。找該分類最近一筆含關鍵字的記錄來改。"""
    body = text.strip()[len(prefix):].strip()
    left, value = _split_colon(body)
    toks = left.split()
    if len(toks) < 3 or not value:
        return Result("修改", _EDIT_HELP)
    cat_word, field_word, keywords = toks[0], toks[-1], toks[1:-1]

    cfg = _cfg_by_word(cat_word)
    if not cfg:
        return Result("修改", f"⚠️ 認不得分類「{cat_word}」。可用:個股新聞 / 產業新聞 / 產業報告 / 全球局勢 / 知識")

    nt = notion_timeline
    if not nt.enabled():
        return Result("修改", "⚠️ 尚未啟用 Notion:請先設定 NOTION_TOKEN。")

    # 目標庫 + 事件類型過濾 + 欄位群
    if cfg.label in ("個股新聞", "產業新聞", "產業報告"):
        ds = nt.TIMELINE_DS
        et = {"個股新聞": "個股新聞", "產業新聞": "產業新聞", "產業報告": "個股產業報告"}[cfg.label]
        group = "timeline"
    elif cfg.label == "全球局勢":
        ds, et, group = nt.GLOBAL_DS, None, "global"
    else:  # 知識
        ds, et, group = nt.KNOWLEDGE_DS, None, "knowledge"

    # 產業報告細項收在內文,無法單獨改
    if cfg.label == "產業報告" and any(w in field_word for w in ("目標價", "券商", "營收", "利多", "利空")):
        return Result("修改", "ℹ️ 產業報告的目標價/券商/營收等細項現在收在「內容(Quick Note)」裡,無法單獨改。\n"
                              "請用「改 產業報告 <關鍵字> 內容:<新內容>」整段更新,或直接到 Notion 編輯。")

    prop, kind = _resolve_edit_field(field_word, group)
    if not prop:
        avail = "、".join(p for _, p, _ in _EDIT_FIELD_MAP[group])
        return Result("修改", f"⚠️ 找不到欄位「{field_word}」。\n可改欄位:{avail}")

    propval, err = _build_edit_prop(kind, value)
    if err:
        return Result("修改", f"⚠️ {err}")

    try:
        pages = nt.recent_pages(ds, 100)
    except nt.NotionError as e:
        return Result("修改", f"⚠️ 讀取失敗:{str(e)[:150]}")

    target = None
    for pg in pages:  # 已新到舊
        if et:
            sel = (pg["properties"].get("事件類型", {}) or {}).get("select") or {}
            if sel.get("name") != et:
                continue
        if all(k in nt.page_to_text(pg) for k in keywords):
            target = pg
            break
    if not target:
        return Result("修改", f"⚠️ 在【{cfg.label}】找不到含「{' '.join(keywords)}」的記錄。")

    old = nt._prop_to_text(target["properties"].get(prop, {}))
    name = nt._prop_to_text(target["properties"].get("Name", {}))
    try:
        nt.set_page_props(target["id"], {prop: propval})
    except nt.NotionError as e:
        return Result("修改", f"⚠️ 更新失敗(可能是選項值不合法):{str(e)[:150]}")
    return Result("修改", f"✅ 已更新【{cfg.label}】「{name}」的「{prop}」:\n{old or '(空)'} → {value}")


# ==========================================================
# 本機快速測試
# ==========================================================
if __name__ == "__main__":
    demo = """個股新聞
光聖(6442)受惠CPO需求爆發,光聖三可轉債將於2026年7月15日掛牌,擴廠產能預計第四季開出。
https://example.com/news/6442"""
    print("正在處理...")
    result = route_and_store(demo)
    print(f"分類:{result.label}")
    print(result.reply)
    print("✅ 已寫入 Notion")

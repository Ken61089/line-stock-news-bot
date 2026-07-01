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
import datetime
from dataclasses import dataclass
from typing import List, Type, Callable

import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
from pydantic import BaseModel, ValidationError, model_validator

from web_fetch import fetch_article, FetchError

# ==========================================================
# 設定
# ==========================================================
AI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://hnd1.aihub.zeabur.ai/v1")
AI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-5")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_TITLE = os.environ.get("GOOGLE_SHEET_TITLE", "股票投資大腦資料庫")

# 送進 AI 分析的內文字數上限(抓到的全文可能很長)
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "8000"))

# 概念股主表分頁(個股 ↔ 概念族群,自動累積)
CONCEPT_MASTER_TAB = os.environ.get("CONCEPT_MASTER_TAB", "概念股主表")

# 概念題材標準清單分頁(白名單;AI 抽概念時優先對照,名稱統一)
CONCEPT_LIST_TAB = os.environ.get("CONCEPT_LIST_TAB", "概念清單")

# 種子清單:第一次會寫進「概念清單」分頁,之後你可在 Sheet 自行增刪(改完需重啟服務生效)
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

_whitelist_cache = None

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
# 分類設定(關鍵字 → 分頁 / 模型 / 提示 / 欄位 / 回覆)
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
    "🛠️ 想更正?「主表」改概念股主表、「改」改某筆新聞、「改股 8111:6442 光聖」一次改遍所有分頁\n"
    "📖 打「說明」看完整指令總表"
)

# 查詢模式的觸發前綴(訊息開頭出現就進查詢,而非寫入)
_QUERY_PREFIXES = ["查詢", "查", "問", "搜尋"]

# 「說明」指令:整段訊息完全等於這些字才觸發(避免新聞內文誤中)
_HELP_WORDS = {"說明", "help", "指令", "幫助", "用法", "選單", "menu", "?", "？"}
_HELP_TEXT = (
    "📖 機器人指令總覽\n"
    "\n"
    "━━ 存新聞 ━━\n"
    "第一行打分類,第二行貼內容或連結:\n"
    "• 個股新聞  • 產業新聞  • 產業報告\n"
    "• 全球局勢  • 知識(或筆記)\n"
    "(只貼連結我會自動抓全文)\n"
    "\n"
    "━━ 查詢 ━━(「查」或「問」開頭)\n"
    "• 查 CPO有哪些股\n"
    "• 查 光聖屬於哪些概念\n"
    "\n"
    "━━ 改錯:股號打錯 ━━\n"
    "• 改股 1514:1815 富喬 → 所有分頁一次改\n"
    "• 主表 改股 1514:1815 富喬 → 只改主表\n"
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
            completion = _get_ai_client().chat.completions.create(
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
# Google Sheets
# ==========================================================
def _open_workbook():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw:
        if not raw.startswith("{"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8")
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)

    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID) if GOOGLE_SHEET_ID else gc.open(GOOGLE_SHEET_TITLE)


def _get_worksheet(tab_name: str, header: List[str]):
    workbook = _open_workbook()

    try:
        worksheet = workbook.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        worksheet = workbook.add_worksheet(title=tab_name, rows=1000, cols=max(len(header), 10))
        worksheet.append_row(header)
        return worksheet

    # 確保標題列正確(新增欄位時會自動補上;只在結尾加欄不會弄亂既有資料)
    if worksheet.row_values(1) != header:
        worksheet.update(values=[header], range_name="A1")
    return worksheet


# ==========================================================
# 概念股主表(概念導向:一列一個概念,右邊列出成分股,自動累積)
# ==========================================================
_CONCEPT_MASTER_HEADER = ["概念/題材", "成分股", "個股數", "首次出現", "最近出現"]
_STOCK_CODE_RE = re.compile(r"\d{3,6}")
_LIST_SPLIT_RE = re.compile(r"[,、,;；/]")


def _stock_key(s: str) -> str:
    """個股的去重鍵:優先用代號(台股 3-6 位數),否則用去空白小寫的名稱。"""
    m = _STOCK_CODE_RE.search(s)
    return m.group(0) if m else re.sub(r"\s+", "", s).lower()


def _split_list(s: str) -> List[str]:
    return [t.strip() for t in _LIST_SPLIT_RE.split(s) if t.strip()]


def _update_concept_master(pairs, now: str) -> str:
    """把 [(個股, [概念...]), ...] 反轉成「概念 → 成分股」upsert 進主表;回傳更新摘要。"""
    # 反轉:concept -> [個股顯示名](以代號去重、保序)
    batch, order = {}, []
    for stock, concepts in pairs or []:
        stock = (stock or "").strip()
        if not stock:
            continue
        for c in concepts or []:
            c = (c or "").strip()
            if not c:
                continue
            if c not in batch:
                batch[c] = []
                order.append(c)
            if not any(_stock_key(stock) == _stock_key(x) for x in batch[c]):
                batch[c].append(stock)
    if not batch:
        return ""

    ws = _get_worksheet(CONCEPT_MASTER_TAB, _CONCEPT_MASTER_HEADER)
    rows = ws.get_all_values()
    existing = {}  # 概念 -> (列號1-based, 該列資料)
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0].strip():
            existing[r[0].strip()] = (i, r)

    updates, appends, lines = [], [], []
    for c in order:
        new_stocks = batch[c]
        if c in existing:
            ridx, r = existing[c]
            cur = _split_list(r[1]) if len(r) > 1 else []
            added = [s for s in new_stocks if not any(_stock_key(s) == _stock_key(x) for x in cur)]
            merged = cur + added
            first = r[3] if (len(r) > 3 and r[3].strip()) else now
            updates.append((f"A{ridx}:E{ridx}", [[c, ", ".join(merged), len(merged), first, now]]))
            note = f"(新增:{', '.join(added)})" if added else "(無新增)"
            lines.append(f"• {c}({len(merged)}檔):{', '.join(merged)} {note}")
        else:
            appends.append([c, ", ".join(new_stocks), len(new_stocks), now, now])
            lines.append(f"• {c}({len(new_stocks)}檔):{', '.join(new_stocks)} (新概念)")

    for rng, vals in updates:
        ws.update(values=vals, range_name=rng, value_input_option="USER_ENTERED")
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")

    return "🏷️ 概念股主表已更新:\n" + "\n".join(lines)


def _get_concept_whitelist() -> List[str]:
    """讀「概念清單」分頁當白名單;不存在則用種子建立。結果快取(改清單需重啟生效)。"""
    global _whitelist_cache
    if _whitelist_cache is not None:
        return _whitelist_cache
    try:
        wb = _open_workbook()
        try:
            ws = wb.worksheet(CONCEPT_LIST_TAB)
            items = [v.strip() for v in ws.col_values(1)[1:] if v.strip()]  # 跳過標題列
            if not items:  # 分頁存在但沒內容 → 補種子
                items = list(_CONCEPT_SEED)
                ws.update(values=[[x] for x in items], range_name="A2")
        except gspread.WorksheetNotFound:
            ws = wb.add_worksheet(title=CONCEPT_LIST_TAB, rows=max(len(_CONCEPT_SEED) + 20, 120), cols=2)
            ws.update(values=[["概念/題材標準名稱"]] + [[x] for x in _CONCEPT_SEED], range_name="A1")
            items = list(_CONCEPT_SEED)
        _whitelist_cache = items
    except Exception as e:  # noqa: BLE001
        logger.warning("讀取概念清單失敗,改用內建種子清單:%s", e)
        _whitelist_cache = list(_CONCEPT_SEED)
    return _whitelist_cache


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
        if not question:
            raise NoCategoryError(
                "🔍 查詢用法:在「查」後面接你的問題。\n例如:\n查 光聖最近有什麼時程\n查 這週的全球局勢重點"
            )
        return _answer_query(question)

    # 再看是不是「修正指令」(主表 / 改),是的話處理、不寫入新聞
    correction = _handle_correction(text)
    if correction is not None:
        return correction

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

    content = content[:MAX_CONTENT_CHARS]
    title = fetched_title or _extract_title(content)
    analysis = _analyze(cfg, title, content)
    now = _now_str()
    worksheet = _get_worksheet(cfg.tab, cfg.header)
    worksheet.append_row(cfg.to_row(analysis, title, url, now), value_input_option="USER_ENTERED")

    reply = cfg.format_reply(analysis)
    # 自動更新「概念股主表」(個股↔概念),失敗不影響存檔
    if cfg.to_concept_pairs:
        try:
            addendum = _update_concept_master(cfg.to_concept_pairs(analysis), now)
            if addendum:
                reply = f"{reply}\n\n{addendum}"
        except Exception as e:  # noqa: BLE001
            logger.warning("更新概念股主表失敗(不影響存檔):%s", e)

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


def _gather_corpus() -> str:
    """把各分頁的近期資料攤平成精簡文字(較新的在前),供 AI 檢索。"""
    workbook = _open_workbook()
    blocks: List[str] = []
    for cfg in ALL_CONFIGS:
        try:
            ws = workbook.worksheet(cfg.tab)
        except gspread.WorksheetNotFound:
            continue
        rows = ws.get_all_values()
        if len(rows) <= 1:
            continue
        header, data = rows[0], rows[1:]
        for r in reversed(data[-QUERY_ROWS_PER_TAB:]):  # 新的在前
            cells = [f"{h}:{v}" for h, v in zip(header, r) if v.strip()]
            if cells:
                blocks.append(f"〔{cfg.label}〕" + " | ".join(cells))

    # 附上概念股主表(個股↔概念對照),方便回答「某檔有哪些概念/某概念有哪些股」
    try:
        ws = workbook.worksheet(CONCEPT_MASTER_TAB)
        rows = ws.get_all_values()
        if len(rows) > 1:
            header = rows[0]
            for r in rows[1:]:
                if r and r[0].strip():
                    cells = [f"{h}:{v}" for h, v in zip(header, r) if v.strip()]
                    blocks.append("〔概念股主表〕" + " | ".join(cells))
    except gspread.WorksheetNotFound:
        pass

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
        f"以下是使用者 Google Sheet 投資筆記資料庫(較新的在前):\n\n{corpus}\n\n"
        f"---\n使用者的問題:{question}\n\n請只根據上面的資料庫內容回答。"
    )
    completion = _get_ai_client().chat.completions.create(
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
    "• 主表 加 <概念>:<個股>       例:主表 加 CPO:6442 光聖\n"
    "• 主表 移除 <概念>:<個股>     例:主表 移除 光通訊:2330\n"
    "• 主表 移除股 <個股>          例:主表 移除股 8111(從所有概念移除)\n"
    "• 主表 改股 <舊>:<新>         例:主表 改股 8111:6442 光聖(股號打錯一次改掉)\n"
    "• 主表 合併 <概念A>:<概念B>   例:主表 合併 共同封裝光學:CPO\n"
    "• 主表 刪除 <概念>            例:主表 刪除 半導體"
)
_EDIT_HELP = (
    "🛠️ 修改某一筆新聞指令:\n"
    "改 <分類> <關鍵字> <欄位>:<新值>\n"
    "例:改 產業報告 全新 目標價:500元\n"
    "(會找該分類中最近一筆含關鍵字的資料列來改)"
)


def _split_colon(s: str):
    """用第一個半形或全形冒號切成 (左, 右)。"""
    idx = min([i for i in (s.find(":"), s.find("：")) if i != -1], default=-1)
    if idx == -1:
        return s.strip(), ""
    return s[:idx].strip(), s[idx + 1:].strip()


def _find_concept_row(rows, concept: str):
    """在主表找概念列(不分大小寫);回傳 (列號1-based, 該列) 或 (None, None)。"""
    target = concept.strip().casefold()
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0].strip().casefold() == target:
            return i, r
    return None, None


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
    # 全面改股(所有分頁)— 需在通用「改」之前判斷
    for p in ("改股", "換股", "全部改股"):
        if first.startswith(p):
            old, new = _split_colon(first[len(p):])
            if not old or not new:
                return Result("改股", "用法:改股 <舊>:<新>(一次修正所有分頁)\n例:改股 8111:6442 光聖")
            return _global_fix_stock(old, new)
    for p in ("修改", "改"):
        if first.startswith(p):
            return _edit_row_command(text, p)
    return None


def _global_fix_stock(old: str, new: str) -> Result:
    """把股號 old 在『所有分頁』(各新聞分頁的個股欄 + 概念股主表成分股)一次換成 new。"""
    now = _now_str()
    oldkey = _stock_key(old)
    wb = _open_workbook()
    report = []

    def _fix_cell(stocks):
        out = []
        for x in stocks:
            x2 = new if _stock_key(x) == oldkey else x
            if not any(_stock_key(x2) == _stock_key(y) for y in out):
                out.append(x2)
        return out

    # 1) 概念股主表(整列 A:E,順便更新個股數與最近出現)
    try:
        mws = wb.worksheet(CONCEPT_MASTER_TAB)
        rows = mws.get_all_values()
        n = 0
        for i, r in enumerate(rows[1:], start=2):
            cur = _split_list(r[1]) if len(r) > 1 else []
            if not any(_stock_key(x) == oldkey for x in cur):
                continue
            newlist = _fix_cell(cur)
            first_seen = r[3] if (len(r) > 3 and r[3].strip()) else now
            mws.update(values=[[r[0].strip(), ", ".join(newlist), len(newlist), first_seen, now]],
                       range_name=f"A{i}:E{i}", value_input_option="USER_ENTERED")
            n += 1
        if n:
            report.append(f"概念股主表:{n} 個概念")
    except gspread.WorksheetNotFound:
        pass

    # 2) 各新聞分頁的個股欄
    for cfg in ALL_CONFIGS:
        try:
            ws = wb.worksheet(cfg.tab)
        except gspread.WorksheetNotFound:
            continue
        rows = ws.get_all_values()
        if not rows:
            continue
        col = next((j for j, h in enumerate(rows[0]) if h.strip() in ("個股", "提及個股")), None)
        if col is None:
            continue
        n = 0
        for i, r in enumerate(rows[1:], start=2):
            if len(r) <= col:
                continue
            stocks = _split_list(r[col])
            if not any(_stock_key(x) == oldkey for x in stocks):
                continue
            ws.update(values=[[", ".join(_fix_cell(stocks))]],
                      range_name=gspread.utils.rowcol_to_a1(i, col + 1), value_input_option="USER_ENTERED")
            n += 1
        if n:
            report.append(f"{cfg.label}:{n} 列")

    if not report:
        return Result("改股", f"⚠️ 所有分頁裡都沒有找到「{old}」")
    return Result("改股", f"✅ 已把「{old}」全面更正為「{new}」:\n" + "\n".join("• " + x for x in report))


def _master_command(text: str) -> Result:
    body = text.strip()[len("主表"):].strip()
    if not body:
        return Result("主表", _MASTER_HELP)
    parts = body.split(None, 1)
    action = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    ws = _get_worksheet(CONCEPT_MASTER_TAB, _CONCEPT_MASTER_HEADER)
    rows = ws.get_all_values()
    now = _now_str()

    if action in ("加", "新增", "加入"):
        concept, stocklist = _split_colon(rest)
        stocks = _split_list(stocklist)
        if not concept or not stocks:
            return Result("主表", "用法:主表 加 <概念>:<個股>\n例:主表 加 CPO:6442 光聖")
        ridx, r = _find_concept_row(rows, concept)
        if ridx:
            cur = _split_list(r[1]) if len(r) > 1 else []
            added = [s for s in stocks if not any(_stock_key(s) == _stock_key(x) for x in cur)]
            merged = cur + added
            first = r[3] if (len(r) > 3 and r[3].strip()) else now
            ws.update(values=[[r[0].strip(), ", ".join(merged), len(merged), first, now]],
                      range_name=f"A{ridx}:E{ridx}", value_input_option="USER_ENTERED")
            return Result("主表", f"✅ 【{r[0].strip()}】加入:{', '.join(added) or '(已存在,無新增)'}\n"
                                   f"現有({len(merged)}檔):{', '.join(merged)}")
        ws.append_row([concept, ", ".join(stocks), len(stocks), now, now], value_input_option="USER_ENTERED")
        return Result("主表", f"✅ 新增概念【{concept}】({len(stocks)}檔):{', '.join(stocks)}")

    if action in ("移除", "刪股", "移除個股"):
        concept, stock = _split_colon(rest)
        if not concept or not stock:
            return Result("主表", "用法:主表 移除 <概念>:<個股>\n例:主表 移除 光通訊:2330")
        ridx, r = _find_concept_row(rows, concept)
        if not ridx:
            return Result("主表", f"⚠️ 找不到概念【{concept}】")
        cur = _split_list(r[1]) if len(r) > 1 else []
        kept = [x for x in cur if _stock_key(x) != _stock_key(stock)]
        if len(kept) == len(cur):
            return Result("主表", f"⚠️ 【{concept}】裡沒有找到「{stock}」")
        if kept:
            first = r[3] if (len(r) > 3 and r[3].strip()) else now
            ws.update(values=[[r[0].strip(), ", ".join(kept), len(kept), first, now]],
                      range_name=f"A{ridx}:E{ridx}", value_input_option="USER_ENTERED")
            return Result("主表", f"✅ 已從【{concept}】移除「{stock}」;剩 {len(kept)} 檔:{', '.join(kept)}")
        ws.delete_rows(ridx)
        return Result("主表", f"✅ 已從【{concept}】移除「{stock}」;該概念已無成分股,整列刪除")

    if action in ("移除股", "全移除"):
        stock = rest.strip()
        if not stock:
            return Result("主表", "用法:主表 移除股 <個股>\n例:主表 移除股 8111")
        touched = []
        for i, r in enumerate(rows[1:], start=2):
            if not r or not r[0].strip():
                continue
            cur = _split_list(r[1]) if len(r) > 1 else []
            kept = [x for x in cur if _stock_key(x) != _stock_key(stock)]
            if len(kept) != len(cur):
                touched.append((i, r[0].strip(), kept, r[3] if len(r) > 3 else now))
        if not touched:
            return Result("主表", f"⚠️ 所有概念裡都沒有找到「{stock}」")
        # 由下往上寫/刪,避免刪列後列號位移
        for i, cname, kept, first in sorted(touched, key=lambda t: -t[0]):
            if kept:
                ws.update(values=[[cname, ", ".join(kept), len(kept), first or now, now]],
                          range_name=f"A{i}:E{i}", value_input_option="USER_ENTERED")
            else:
                ws.delete_rows(i)
        return Result("主表", f"✅ 已從 {len(touched)} 個概念移除「{stock}」:{', '.join(t[1] for t in touched)}")

    if action in ("改股", "換股", "更正股", "改代號"):
        old, new = _split_colon(rest)
        if not old or not new:
            return Result("主表", "用法:主表 改股 <舊>:<新>\n例:主表 改股 8111:6442 光聖")
        oldkey = _stock_key(old)
        touched = []
        for i, r in enumerate(rows[1:], start=2):
            if not r or not r[0].strip():
                continue
            cur = _split_list(r[1]) if len(r) > 1 else []
            if not any(_stock_key(x) == oldkey for x in cur):
                continue
            newlist = []
            for x in cur:
                x2 = new if _stock_key(x) == oldkey else x
                if not any(_stock_key(x2) == _stock_key(y) for y in newlist):
                    newlist.append(x2)
            first = r[3] if (len(r) > 3 and r[3].strip()) else now
            touched.append((i, r[0].strip(), newlist, first))
        if not touched:
            return Result("主表", f"⚠️ 主表裡沒有任何概念含「{old}」")
        for i, cname, newlist, first in touched:
            ws.update(values=[[cname, ", ".join(newlist), len(newlist), first, now]],
                      range_name=f"A{i}:E{i}", value_input_option="USER_ENTERED")
        return Result("主表", f"✅ 已把「{old}」更正為「{new}」,更新 {len(touched)} 個概念:"
                              f"{', '.join(t[1] for t in touched)}")

    if action in ("合併", "併入", "改名"):
        a, b = _split_colon(rest)
        if not a or not b:
            return Result("主表", "用法:主表 合併 <概念A>:<概念B>(A 併入 B)\n例:主表 合併 共同封裝光學:CPO")
        ra, rowa = _find_concept_row(rows, a)
        if not ra:
            return Result("主表", f"⚠️ 找不到來源概念【{a}】")
        astocks = _split_list(rowa[1]) if len(rowa) > 1 else []
        afirst = rowa[3] if len(rowa) > 3 else now
        rb, rowb = _find_concept_row(rows, b)
        if rb:
            bstocks = _split_list(rowb[1]) if len(rowb) > 1 else []
            added = [s for s in astocks if not any(_stock_key(s) == _stock_key(x) for x in bstocks)]
            merged = bstocks + added
            bfirst = rowb[3] if (len(rowb) > 3 and rowb[3].strip()) else now
            first = min(x for x in (bfirst, afirst) if x) if (bfirst or afirst) else now
            ws.update(values=[[rowb[0].strip(), ", ".join(merged), len(merged), first, now]],
                      range_name=f"A{rb}:E{rb}", value_input_option="USER_ENTERED")
            ws.delete_rows(ra)
            return Result("主表", f"✅ 已把【{a}】併入【{rowb[0].strip()}】,共 {len(merged)} 檔:{', '.join(merged)}")
        ws.update(values=[[b]], range_name=f"A{ra}", value_input_option="USER_ENTERED")
        return Result("主表", f"✅ 已把概念【{a}】改名為【{b}】")

    if action in ("刪除", "刪", "刪概念"):
        concept = rest.strip()
        ridx, r = _find_concept_row(rows, concept)
        if not ridx:
            return Result("主表", f"⚠️ 找不到概念【{concept}】")
        n_stock = len(_split_list(r[1])) if len(r) > 1 else 0
        ws.delete_rows(ridx)
        return Result("主表", f"✅ 已刪除概念【{r[0].strip()}】(原 {n_stock} 檔)")

    return Result("主表", f"❓ 不認得的動作「{action}」。\n\n{_MASTER_HELP}")


def _edit_row_command(text: str, prefix: str) -> Result:
    body = text.strip()[len(prefix):].strip()
    left, value = _split_colon(body)
    toks = left.split()
    if len(toks) < 3 or not value:
        return Result("修改", _EDIT_HELP)
    cat_word, field_word, keywords = toks[0], toks[-1], toks[1:-1]

    cfg = _cfg_by_word(cat_word)
    if not cfg:
        return Result("修改", f"⚠️ 認不得分類「{cat_word}」。可用:個股新聞 / 產業新聞 / 產業報告 / 全球局勢 / 知識")

    ws = _get_worksheet(cfg.tab, cfg.header)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return Result("修改", f"⚠️ 【{cfg.label}】還沒有資料。")
    header = rows[0]
    col_idx = next((i for i, h in enumerate(header) if field_word in h), None)
    if col_idx is None:
        return Result("修改", f"⚠️ 找不到欄位「{field_word}」。\n可用欄位:{', '.join(header)}")

    target_i, target_row = None, None
    for i in range(len(rows) - 1, 0, -1):  # 由最新往回找
        if all(k in " ".join(rows[i]) for k in keywords):
            target_i, target_row = i + 1, rows[i]
            break
    if target_i is None:
        return Result("修改", f"⚠️ 在【{cfg.label}】找不到含「{' '.join(keywords)}」的資料列。")

    old = target_row[col_idx] if col_idx < len(target_row) else ""
    a1 = gspread.utils.rowcol_to_a1(target_i, col_idx + 1)
    ws.update(values=[[value]], range_name=a1, value_input_option="USER_ENTERED")
    return Result("修改", f"✅ 已更新【{cfg.label}】第 {target_i} 列「{header[col_idx]}」:\n"
                          f"{old or '(空)'} → {value}")


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
    print("✅ 已寫入 Google Sheet")

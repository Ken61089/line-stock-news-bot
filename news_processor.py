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


INDIVIDUAL = CategoryConfig(
    label="個股新聞",
    tab="新聞時程動態庫",
    header=["處理時間", "新聞標題", "AI 核心摘要", "提及個股", "概念族群分類", "關鍵時程與事件", "新聞原文/連結", "市場/地區"],
    model=StockNews,
    schema_hint='{"summary":"60-100字摘要","market":"主要市場,如 台股/美股/日股/港股,多個用、分隔","mentioned_stocks":["6442 光聖"],"concept_groups":["CPO","矽光子"],"timelines":[{"date":"2026-07-15","event":"可轉債掛牌"}]}',
    task="1.撰寫60-100字投資人摘要。2.判斷主要涉及的股票市場/地區(台股/美股/日股/港股等)。3.精確提取個股與代號。4.辨別相關科技概念股/供應鏈族群。5.抽取所有未來關鍵時程。",
    to_row=lambda a, title, url, now: [now, title, a.summary, _join(a.mentioned_stocks), _join(a.concept_groups), _fmt_timeline(a.timelines), url, a.market],
    format_reply=lambda a: (
        "✅ 已寫入【個股新聞】\n\n"
        f"📌 摘要:{a.summary}\n"
        f"🌍 市場:{a.market or '(未標明)'}\n"
        f"📈 個股:{_join(a.mentioned_stocks) or '(無)'}\n"
        f"🏷️ 族群:{_join(a.concept_groups) or '(無)'}\n"
        f"🗓️ 時程:\n{_fmt_timeline(a.timelines) or '  (無明確時程)'}"
    ),
)

INDUSTRY = CategoryConfig(
    label="產業新聞",
    tab="產業動態",
    header=["處理時間", "新聞標題", "AI 核心摘要", "相關產業/族群", "重點趨勢", "提及個股", "新聞原文/連結"],
    model=IndustryNews,
    schema_hint='{"summary":"60-100字摘要","industry_groups":["先進封裝","散熱"],"key_trends":["趨勢一","趨勢二"],"mentioned_stocks":["2330 台積電"]}',
    task="這是產業新聞。1.撰寫60-100字摘要。2.辨別相關產業/供應鏈族群。3.整理出重點趨勢(條列,每點一句)。4.提取文中提及的個股。",
    to_row=lambda a, title, url, now: [now, title, a.summary, _join(a.industry_groups), _fmt_bullets(a.key_trends), _join(a.mentioned_stocks), url],
    format_reply=lambda a: (
        "✅ 已寫入【產業新聞】\n\n"
        f"📌 摘要:{a.summary}\n"
        f"🏭 產業/族群:{_join(a.industry_groups) or '(無)'}\n"
        f"📊 重點趨勢:\n{_fmt_bullets(a.key_trends) or '  (無)'}\n"
        f"📈 個股:{_join(a.mentioned_stocks) or '(無)'}"
    ),
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
    header=["處理時間", "個股", "報告日期", "出具券商", "券商目標價", "近期營收", "時間軸", "報告總結", "報告原文/連結"],
    model=IndustryReport,
    schema_hint='{"stocks":["2330 台積電"],"report_date":"2026-06-27","broker":"摩根士丹利","target_price":"1200元","recent_revenue":"5月營收月增12%、年增20%","timelines":[{"date":"2026-07-16","event":"法說會"}],"summary":"80-150字報告總結","catalysts":["先進製程調漲","切入NVIDIA供應鏈","CoWoS擴產"],"risks":["匯率逆風","記憶體報價回落"]}',
    task=(
        "這是券商/分析師出具的個股研究報告(或法人報告、產業深度報告)。請結構化整理:"
        "1.報告涵蓋的個股與代號(stocks)。"
        "2.報告日期(report_date,YYYY-MM-DD;若只寫月/日,用今天日期推斷正確年份)。"
        "3.出具報告的券商/機構(broker)。"
        "4.券商給的目標價(target_price,含單位,如「1200元」;沒有就留空)。"
        "5.近期營收狀況(recent_revenue,如月營收年增率、季營收、毛利率等具體數字)。"
        "6.關鍵時程(timelines,如法說會、新廠投產、新品量產、訂單交付)。"
        "7.報告總結(summary,80-150字,寫給投資人看的重點)。"
        "8.【特別重要】利多訊號(catalysts):請對任何看起來會『帶動營收成長或公司轉型』的字眼高度敏感、寧多勿漏,"
        "例如:漲價/調漲/報價上揚、新增客戶/拿下大單、打入或切入某供應鏈/通過認證、擴產/擴廠/擴充產能、"
        "資本支出增加/上修、轉型、產能滿載/利用率提升、訂單能見度高、急單、供不應求、毛利率提升、新產品/新應用等。"
        "9.利空訊號(risks):同樣對反向字眼敏感,例如:降價/殺價/報價下滑、砍單/掉單、客戶流失/轉單、"
        "產能利用率下降、資本支出縮減/遞延、需求疲弱、庫存調整去化、毛利率下滑、認證未過/出貨遞延等。"
        "catalysts 與 risks 都用簡短詞組條列,每點抓住關鍵(可帶一點原文數字)。"
    ),
    to_row=lambda a, title, url, now: [
        now, _join(a.stocks), a.report_date, a.broker, a.target_price,
        a.recent_revenue, _fmt_timeline(a.timelines), _fmt_report_summary(a), url,
    ],
    format_reply=lambda a: (
        "✅ 已寫入【產業報告】\n\n"
        f"📈 個股:{_join(a.stocks) or '(未標明)'}\n"
        f"🏦 券商:{a.broker or '(未標明)'}\n"
        f"🎯 目標價:{a.target_price or '(無)'}\n"
        f"💰 近期營收:{a.recent_revenue or '(無)'}\n"
        f"📅 報告日期:{a.report_date or '(無)'}\n"
        f"📝 總結:{a.summary or '(無)'}\n"
        f"🟢 利多:{_join(a.catalysts) or '(無)'}\n"
        f"🔴 利空:{_join(a.risks) or '(無)'}\n"
        f"🗓️ 時間軸:\n{_fmt_timeline(a.timelines) or '  (無明確時程)'}"
    ),
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
    "🔍 想查資料庫?用「查」開頭,例如:查 光聖最近的時程"
)

# 查詢模式的觸發前綴(訊息開頭出現就進查詢,而非寫入)
_QUERY_PREFIXES = ["查詢", "查", "問", "搜尋"]


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
# 對外的一站式函數:給 Telegram/LINE 機器人呼叫
# ==========================================================
def route_and_store(text: str) -> Result:
    # 先看是不是「查詢」(用「查」「問」等開頭),是的話走查詢、不寫入
    question = _detect_query(text)
    if question is not None:
        if not question:
            raise NoCategoryError(
                "🔍 查詢用法:在「查」後面接你的問題。\n例如:\n查 光聖最近有什麼時程\n查 這週的全球局勢重點"
            )
        return _answer_query(question)

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
    return Result(label=cfg.label, reply=cfg.format_reply(analysis))


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

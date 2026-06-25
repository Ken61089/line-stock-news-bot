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

# ==========================================================
# 設定
# ==========================================================
AI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://hnd1.aihub.zeabur.ai/v1")
AI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-5")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_TITLE = os.environ.get("GOOGLE_SHEET_TITLE", "股票投資大腦資料庫")

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


# ==========================================================
# 格式化小工具
# ==========================================================
def _fmt_timeline(timelines: List[TimelineEvent]) -> str:
    return "\n".join(f"[{i}] {t.date} -> {t.event}" for i, t in enumerate(timelines, 1)).strip()


def _fmt_bullets(items: List[str]) -> str:
    return "\n".join(f"• {x}" for x in items).strip()


def _join(items: List[str]) -> str:
    return ", ".join(items)


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

# 關鍵字 → 分類(含別名);依長度由長到短比對,避免「個股」先吃掉「個股新聞」
_KEYWORDS = [
    ("個股新聞", INDIVIDUAL), ("個股", INDIVIDUAL),
    ("產業新聞", INDUSTRY), ("產業", INDUSTRY),
    ("全球局勢新聞", GLOBAL), ("全球局勢", GLOBAL), ("全球", GLOBAL), ("總經", GLOBAL), ("國際", GLOBAL),
    ("知識補充", KNOWLEDGE), ("知識", KNOWLEDGE), ("筆記", KNOWLEDGE), ("觀念", KNOWLEDGE),
]
_KEYWORDS.sort(key=lambda kv: len(kv[0]), reverse=True)

GUIDANCE = (
    "⚠️ 請在第一行標上分類關鍵字,第二行起貼內容。\n\n"
    "可用分類:\n"
    "• 個股新聞\n• 產業新聞\n• 全球局勢\n• 知識(或筆記)\n\n"
    "範例:\n個股新聞\n光聖(6442)受惠CPO需求爆發…"
)


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
def _get_worksheet(tab_name: str, header: List[str]):
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
    workbook = gc.open_by_key(GOOGLE_SHEET_ID) if GOOGLE_SHEET_ID else gc.open(GOOGLE_SHEET_TITLE)

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
    cfg, content = detect_category(text)
    if cfg is None:
        raise NoCategoryError(GUIDANCE)
    if len(content.strip()) < 12:
        raise NoCategoryError(f"已辨識分類『{cfg.label}』,但下面沒看到內容。請在關鍵字的下一行貼上要記錄的內容。")

    title, url = _extract_title_and_url(content)
    analysis = _analyze(cfg, title, content)
    now = _now_str()
    worksheet = _get_worksheet(cfg.tab, cfg.header)
    worksheet.append_row(cfg.to_row(analysis, title, url, now), value_input_option="USER_ENTERED")
    return Result(label=cfg.label, reply=cfg.format_reply(analysis))


def _extract_title_and_url(text: str):
    url_match = re.search(r"https?://\S+", text)
    url = url_match.group(0) if url_match else ""
    title = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("http"):
            title = line[:120]
            break
    if not title:
        title = (text.strip()[:120] or "(未命名)")
    return title, url


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

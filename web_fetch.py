"""
抓取網頁文章全文。

給機器人用:使用者只丟一個新聞連結時,自動把整篇文章的正文抓下來,
再交給 Claude 分析,省去手動複製整段內文。

抓取策略(自動選用,失敗會往下退):
  1. **trafilatura 優先**(免費、零設定,專門抽正文,對一般新聞網站又快又準)
  2. 抓不到或內容過短 → 才用 Firecrawl(較會處理動態/難搞網站,但要花額度)

⚠️ **順序刻意是 trafilatura 先**(2026-08-11 改)。原本 Firecrawl 優先,結果:
MoneyDJ 那類頁面 `onlyMainContent:true` 對它無效,回傳整頁 6 萬字(導覽列、選單、
相關新聞全在裡面),而正文出現在第 5.4 萬字 —— 被 MAX_ARTICLE_CHARS 截斷後
**只剩導覽列送進 AI**,摘要當然生不出東西(同一篇本機走 trafilatura 只抽 818 字精準正文)。
順帶也省 Firecrawl 額度(與 LINE 機器人的每日抓取共用同一份)。

抓不到時丟 FetchError,呼叫端應退回「請使用者直接貼內文」的行為。
"""

import os
import logging

import httpx

logger = logging.getLogger("line-news-bot")

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_BASE_URL = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/")

# 抓回來的正文上限(字元),避免超長文章吃掉過多 AI token
MAX_ARTICLE_CHARS = int(os.environ.get("MAX_ARTICLE_CHARS", "8000"))
# 低於這個字數就當作「沒抓到正文」。一般新聞至少 300 字;抓到 100 字通常是
# cookie 提示、登入牆或只有標題,硬送 AI 只會生出一篇編的摘要。
MIN_ARTICLE_CHARS = int(os.environ.get("MIN_ARTICLE_CHARS", "150"))


class FetchError(Exception):
    """抓取或抽取內文失敗(網頁需登入、擋爬蟲、動態載入等)。"""


def fetch_article(url: str) -> tuple[str, str]:
    """回傳 (title, text)。兩者皆為純文字;抓不到或內容不足以分析時丟 FetchError。"""
    title, text, used = "", "", ""
    try:
        title, text = _fetch_trafilatura(url)
        used = "trafilatura"
    except (FetchError, Exception) as e:  # noqa: BLE001
        logger.info("trafilatura 抓不到(%s)", e)

    # trafilatura 沒抓到、或只抓到零星幾句 → 才動用 Firecrawl(要花額度)
    if len(text.strip()) < MIN_ARTICLE_CHARS and FIRECRAWL_API_KEY:
        try:
            t2, x2 = _fetch_firecrawl(url)
            if len(x2.strip()) > len(text.strip()):
                title, text, used = (t2 or title), x2, "firecrawl"
        except Exception as e:  # noqa: BLE001
            logger.warning("Firecrawl 抓取也失敗:%s", e)

    text = text.strip()
    logger.info("抓文完成(%s):%d 字 ← %s", used or "無", len(text), url[:60])
    if len(text) < MIN_ARTICLE_CHARS:
        raise FetchError(
            f"只抓到 {len(text)} 字,不足以分析(可能被擋爬蟲、需登入,或正文是動態載入)"
        )
    return title.strip(), text[:MAX_ARTICLE_CHARS]


def _fetch_firecrawl(url: str) -> tuple[str, str]:
    # ⚠️ `onlyMainContent` 對某些站(如 MoneyDJ)沒有效果,還是會把導覽列/選單/相關新聞
    # 整包回來,所以額外用 excludeTags 把版面元素砍掉,免得正文被擠到截斷點之後。
    resp = httpx.post(
        f"{FIRECRAWL_BASE_URL}/v2/scrape",
        headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
        json={"url": url, "formats": ["markdown"], "onlyMainContent": True,
              "excludeTags": ["nav", "header", "footer", "aside", "script", "style", "form"]},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {}) or {}
    text = (data.get("markdown") or "").strip()
    title = ((data.get("metadata") or {}).get("title") or "").strip()
    if not text:
        raise FetchError("Firecrawl 回傳空內容")
    return title, text


def _fetch_trafilatura(url: str) -> tuple[str, str]:
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise FetchError(f"無法下載網頁(可能擋爬蟲或網址失效):{url}")

    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text or not text.strip():
        raise FetchError("無法從網頁抽取內文(可能需要登入或為動態頁面)")

    title = ""
    try:
        meta = trafilatura.extract_metadata(downloaded)
        title = (meta.title if meta else "") or ""
    except Exception:  # noqa: BLE001
        pass
    return title, text.strip()

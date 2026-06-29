"""
抓取網頁文章全文。

給機器人用:使用者只丟一個新聞連結時,自動把整篇文章的正文抓下來,
再交給 Claude 分析,省去手動複製整段內文。

抓取策略(自動選用,失敗會往下退):
  1. 若有設 FIRECRAWL_API_KEY → 優先用 Firecrawl(較會處理動態/難搞網站)
  2. 否則用 trafilatura(免費、零設定,對一般新聞網站抽正文很準)

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


class FetchError(Exception):
    """抓取或抽取內文失敗(網頁需登入、擋爬蟲、動態載入等)。"""


def fetch_article(url: str) -> tuple[str, str]:
    """回傳 (title, text)。兩者皆為純文字;失敗丟 FetchError。"""
    title, text = "", ""
    if FIRECRAWL_API_KEY:
        try:
            title, text = _fetch_firecrawl(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("Firecrawl 抓取失敗,改用 trafilatura:%s", e)
    if not text:
        title, text = _fetch_trafilatura(url)

    text = text.strip()[:MAX_ARTICLE_CHARS]
    if not text:
        raise FetchError("抓到網頁但抽不到正文")
    return title.strip(), text


def _fetch_firecrawl(url: str) -> tuple[str, str]:
    resp = httpx.post(
        f"{FIRECRAWL_BASE_URL}/v2/scrape",
        headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
        json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
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

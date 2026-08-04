"""財報 EPS 自動抓取與單季換算。

資料來源(純程式解析,不用 AI、不吃 Firecrawl 額度):
  ① **MOPS 重訊詳細頁** —— 「董事會通過某季財務報告」這則重訊點進去的內容。
     欄位是固定編號格式(3.報導期間起訖 / 4.累計營收 / 10.累計基本每股盈餘 ...),
     正則就抽得到。即時性最好:董事會一過、公告一出就有,不必等財報彙總表。
  ② **MOPS t163sb04 全市場季報彙總** —— 一季一次請求就拿到全上市(或全上櫃)的
     累計每股盈餘。用途有二:回補歷史 8 季、以及補「沒發重訊」的公司
     (有些公司只在期限內申報財報、不另發重訊,只靠 ① 會漏掉)。

⚠️ **季報申報的每股盈餘本來就是「1月1日累計至本期止」的累計數**,
   所以單季 EPS = 本季累計 − 前一季累計;Q1 累計即單季;Q4 沒有「第四季重訊」,
   年報期間是 01/01-12/31,故 Q4 = 年度累計 − Q3 累計。
⚠️ 累計相減得到的單季 EPS 在**當年度有現金增資/配股**時會失真(前後季加權平均股數
   不同,EPS 不可線性相減)。營收與淨利相減則不受影響,可拿來對照。
"""

import os
import re
import time
import logging
import datetime

import httpx

import notion_timeline as nt

logger = logging.getLogger(__name__)

MOPS_DETAIL_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05sr01_1"
MOPS_BULK_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04"
_UA = {"User-Agent": "Mozilla/5.0"}

# 命中條件:主旨同時提到「財務報(告|表)」與「通過」。
# 實際主旨長相:「公告本公司115年度第二季合併財務報告業經董事會決議通過」
#               「公告本公司董事會通過115年第2季合併財務報告」
# ⚠️同一天還有大量「財務報告董事會**召開日期**/預計召開日期」的預告(115/08/04 那天 617 則
#   重訊裡財報相關 333 則、其中多數是預告),那些點進去沒有數字,必須用「通過」+排除「召開」
#   濾掉,否則會白抓一堆詳細頁。
_FIN_SUBJ_RE = re.compile(r"財務報(?:告|表)")
_FIN_EXCLUDE_RE = re.compile(r"召開|預計|財務預測|自結")


def is_financial_report_subject(subject: str) -> bool:
    s = (subject or "").replace(" ", "")
    if not _FIN_SUBJ_RE.search(s) or "通過" not in s:
        return False
    return not _FIN_EXCLUDE_RE.search(s)


# ---------------------------------------------------------------- 詳細頁

def _post_mops(url: str, data: dict, *, timeout: int = 60, tries: int = 3) -> str:
    """POST MOPS 並自動重試,回 decode 好的 HTML。
    政府站會偶發 connection reset / 限流,裸呼叫單次抖動就整個任務失敗。"""
    last: Exception | None = None
    for i in range(tries):
        if i:
            time.sleep(2.0 * i)
        try:
            r = httpx.post(url, data=data, headers=_UA, timeout=timeout)
            r.raise_for_status()
            return r.content.decode("utf-8", "replace")
        except httpx.HTTPError as e:
            last = e
            logger.warning("MOPS POST 第 %d 次失敗(%s):%s", i + 1, url[-20:], e)
    raise last if last else RuntimeError(f"MOPS POST 失敗:{url}")


def fetch_announcement_detail(row: dict) -> str:
    """抓某一則重訊的詳細內容 HTML。row 需含 skey/company_id/spoke_date/spoke_time/seq_no
    (由 fetchers.fetch_mops_announcements 一併帶出)。"""
    data = {
        "step": "1", "TYPEK": "all", "firstin": "true",
        "skey": row.get("skey", ""),
        "hhc_co_name": "",
        "COMPANY_ID": row.get("company_id", "") or row.get("code", ""),
        "COMPANY_NAME": "",
        "SPOKE_DATE": row.get("spoke_date", ""),
        "SPOKE_TIME": row.get("spoke_time", ""),
        "SEQ_NO": row.get("seq_no", ""),
    }
    return _post_mops(MOPS_DETAIL_URL, data, timeout=40)


def _find_num(html: str, label: str):
    """抓「…label…:數字」。MOPS 各業別的欄位編號不同,所以認文字不認編號。"""
    m = re.search(label + r"[^:：]{0,20}[:：]\s*([\-\d,\.]+)", html)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


_PERIOD_RE = re.compile(r"(\d{2,3})/(\d{2})/(\d{2})\s*[~\-–至]\s*(\d{2,3})/(\d{2})/(\d{2})")


def _period_to_year_quarter(text: str):
    """從報導期間起訖(民國)判斷 (西元年, 季)。認結束月:3→Q1 6→Q2 9→Q3 12→Q4。
    非曆年制或格式異常回 (None, None, '')。"""
    m = _PERIOD_RE.search(text or "")
    if not m:
        return None, None, ""
    y1, m1, d1, y2, m2, d2 = m.groups()
    period = f"{y1}/{m1}/{d1}-{y2}/{m2}/{d2}"
    end_m = int(m2)
    if end_m not in (3, 6, 9, 12):
        return None, None, period
    return int(y2) + 1911, end_m // 3, period


def parse_eps_detail(html: str) -> dict | None:
    """從重訊詳細頁抽財報數字。回 {'year','quarter','period','cum_eps','cum_rev','cum_ni'}。
    抽不到報導期間或 EPS 就回 None(代表不是財報通過那類重訊,或格式不符)。"""
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"&nbsp;", " ", text)
    # 只取「報導期間」那行,避免抓到事實發生日等其他日期
    m = re.search(r"報導期間[^\n]*\n?[^\n]*", text)
    year, quarter, period = _period_to_year_quarter(m.group(0) if m else "")
    if not year:
        return None
    cum_eps = _find_num(text, r"累計至本期止基本每股盈餘")
    if cum_eps is None:
        cum_eps = _find_num(text, r"基本每股盈餘")
    if cum_eps is None:
        return None
    cum_rev = _find_num(text, r"累計至本期止營業收入")
    cum_ni = _find_num(text, r"累計至本期止歸屬於母公司業主淨利")
    if cum_ni is None:
        cum_ni = _find_num(text, r"累計至本期止本期淨利")
    return {
        "year": year,
        "quarter": quarter,
        "period": period,
        "cum_eps": cum_eps,
        "cum_rev": cum_rev / 100000 if cum_rev is not None else None,  # 仟元 → 億元
        "cum_ni": cum_ni / 100000 if cum_ni is not None else None,
    }


# ---------------------------------------------------------------- 全市場彙總(回補/補漏)

_BULK_HEAD_RE = re.compile(r"<tr[^>]*class=['\"]?tblHead['\"]?[^>]*>.*?</tr>", re.S)
_BULK_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_BULK_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def _cells(html_fragment: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip()
            for c in _BULK_TD_RE.findall(html_fragment)]


def _num(s: str):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def fetch_bulk_eps(year: int, quarter: int, typek: str = "sii") -> dict:
    """抓 MOPS 綜合損益表彙總(t163sb04),回 {code: {'name','eps','ni'}}(ni=歸屬母公司淨利,億元)。
    year 給西元;typek: sii=上市 otc=上櫃。該季尚未公布時回空 dict。
    ⚠️ 同一頁依業別分成好幾張表(一般業 30 欄、金融/證券/保險 18~23 欄),欄位位置**都不一樣**:
       「淨利(淨損)歸屬於母公司業主」在第 11~23 欄之間浮動。所以逐張表讀自己的表頭定位欄位,
       不能寫死索引;唯一穩定的只有「基本每股盈餘」永遠是最後一欄。"""
    data = {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
            "TYPEK": typek, "year": str(int(year) - 1911), "season": f"{int(quarter):02d}"}
    html = _post_mops(MOPS_BULK_URL, data, timeout=90)

    out: dict = {}
    heads = list(_BULK_HEAD_RE.finditer(html))
    for i, h in enumerate(heads):
        cols = _cells(h.group(0))
        block = html[h.end():(heads[i + 1].start() if i + 1 < len(heads) else len(html))]
        eps_i = next((j for j, c in enumerate(cols) if "基本每股盈餘" in c), None)
        # 認「歸屬於母公司業主」的淨利,排除「綜合損益總額歸屬於母公司業主」(那含未實現損益,
        # 不是 EPS 的分子)
        ni_i = next((j for j, c in enumerate(cols)
                     if "歸屬於母公司業主" in c and "淨利" in c and "綜合損益" not in c), None)
        if eps_i is None:
            continue
        for row in _BULK_ROW_RE.findall(block):
            cells = _cells(row)
            if len(cells) != len(cols):
                continue
            code = cells[0]
            if not re.fullmatch(r"\d{4}", code) or code in out:  # 多張表時以第一次出現為準
                continue
            eps = _num(cells[eps_i])
            if eps is None:
                continue
            ni = _num(cells[ni_i]) if ni_i is not None else None
            out[code] = {"name": cells[1], "eps": eps,
                         "ni": ni / 100000 if ni is not None else None}  # 仟元 → 億元
    return out


# ---------------------------------------------------------------- 單季換算

def _prev_quarter(year: int, quarter: int):
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def recompute_quarterly(code: str, rows: list[dict] | None = None) -> int:
    """重算某檔所有季的單季值(EPS/營收/淨利)並回寫有變動的列,回傳更新筆數。
    Q1 單季=累計;Q2~Q4 單季=本季累計−前季累計(前季缺資料就留空)。"""
    rows = rows if rows is not None else nt.list_eps(code=code)
    by_q = {(r["year"], r["quarter"]): r for r in rows if r["year"] and r["quarter"]}
    updated = 0
    for (y, q), r in by_q.items():
        prev = by_q.get(_prev_quarter(y, q)) if q > 1 else None
        vals = {}
        for key, cum_key in (("q_eps", "cum_eps"), ("q_rev", "cum_rev"), ("q_ni", "cum_ni")):
            cum = r.get(cum_key)
            if cum is None:
                vals[key] = None
            elif q == 1:
                vals[key] = cum
            elif prev and prev.get(cum_key) is not None:
                vals[key] = round(cum - prev[cum_key], 4)
            else:
                vals[key] = None
        changed = any(
            vals[k] is not None and (r.get(k) is None or abs(r[k] - vals[k]) > 1e-6)
            for k in vals
        )
        if not changed:
            continue
        nt.upsert_eps(code, r.get("name", ""), y, q,
                      q_eps=vals["q_eps"], q_rev=vals["q_rev"], q_ni=vals["q_ni"],
                      existing=r)
        updated += 1
    return updated


# ---------------------------------------------------------------- 對外流程

def _stock_index() -> dict:
    """{code: {'id','label','name'}},只含 Notion 個股主表(追蹤股)。"""
    idx = {}
    for s in nt.list_stocks():
        if not s["code"]:
            continue
        name = s["label"].replace(s["code"], "", 1).strip()
        idx[s["code"]] = {"id": s["id"], "label": s["label"], "name": name}
    return idx


def handle_financial_announcements(rows: list[dict], stock_idx: dict | None = None) -> dict:
    """從一批 MOPS 重訊挑出「財報通過」類、抓詳細頁、寫進 EPS 庫並重算單季。
    rows 需含 code/name/subject/date 與詳細頁參數。回傳 {'checked','saved','codes'}。"""
    idx = stock_idx if stock_idx is not None else _stock_index()
    targets = [r for r in rows
               if r.get("code") in idx and is_financial_report_subject(r.get("subject", ""))]
    saved, touched = 0, []
    for r in targets:
        code = r["code"]
        try:
            detail = parse_eps_detail(fetch_announcement_detail(r))
        except (httpx.HTTPError, RuntimeError) as e:
            logger.warning("抓重訊詳細頁失敗(%s):%s", code, e)
            continue
        if not detail:
            logger.info("重訊非財報格式或抽不到 EPS,略過:%s %s", code, r.get("subject", "")[:40])
            continue
        st = idx[code]
        try:
            nt.upsert_eps(
                code, st["name"] or r.get("name", ""), detail["year"], detail["quarter"],
                cum_eps=detail["cum_eps"], cum_rev=detail["cum_rev"], cum_ni=detail["cum_ni"],
                period=detail["period"],
                ann_date=_roc_to_iso(r.get("date", "")),
                source="重訊詳細頁", stock_page_id=st["id"],
                note=r.get("subject", "")[:200],
            )
            saved += 1
            touched.append(code)
        except nt.NotionError as e:
            logger.warning("寫入 EPS 失敗(%s):%s", code, e)
        time.sleep(0.4)  # 對 MOPS 客氣一點
    for code in sorted(set(touched)):
        try:
            recompute_quarterly(code)
        except nt.NotionError as e:
            logger.warning("重算單季失敗(%s):%s", code, e)
    return {"checked": len(targets), "saved": saved, "codes": sorted(set(touched))}


def _roc_to_iso(roc: str) -> str:
    try:
        y, m, d = roc.split("/")
        return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return ""


def recent_quarters(n: int = 8, today: datetime.date | None = None) -> list[tuple]:
    """回最近 n 個「應該已公布」的季,新到舊 [(year,quarter)]。
    申報期限:Q1 5/15、Q2 8/14、Q3 11/14、年報(Q4)3/31,各加 5 天緩衝。"""
    today = today or datetime.date.today()
    # (申報月, 日) + 5 天緩衝;Q4(年報)是隔年 3/31
    deadlines = {1: (5, 20), 2: (8, 19), 3: (11, 19), 4: (4, 5)}
    cand = []
    for yy in range(today.year, today.year - 4, -1):
        for qq in (4, 3, 2, 1):
            mm, dd = deadlines[qq]
            due = datetime.date(yy + 1 if qq == 4 else yy, mm, dd)
            if due <= today:
                cand.append((yy, qq))
    cand.sort(reverse=True)
    return cand[:n]


def backfill(quarters: int = 8, codes: list[str] | None = None,
             overwrite: bool = False, qs: list[tuple] | None = None) -> dict:
    """用全市場季報彙總回補追蹤股近 N 季的累計 EPS,再重算單季。
    overwrite=False 時只補「該季還沒有累計EPS」的列(不覆蓋重訊抓到的精確值)。
    qs 可直接指定要補哪幾季 [(year,quarter)];未給則取最近 quarters 個已過期限的季。
    回傳 {'quarters','filled','skipped','stocks'}。"""
    idx = _stock_index()
    if codes:
        idx = {c: v for c, v in idx.items() if c in codes}
    if not idx:
        return {"quarters": 0, "filled": 0, "skipped": 0, "stocks": 0}

    qs = qs if qs is not None else recent_quarters(quarters)
    existing = {}
    for code in idx:
        for r in nt.list_eps(code=code):
            existing[(code, r["year"], r["quarter"])] = r

    filled = skipped = 0
    for y, q in qs:
        bulk = {}
        for typek in ("sii", "otc"):
            try:
                bulk.update(fetch_bulk_eps(y, q, typek))
            except (httpx.HTTPError, RuntimeError) as e:
                logger.warning("抓 %dQ%d %s 彙總失敗:%s", y, q, typek, e)
            time.sleep(1.0)
        if not bulk:
            logger.info("%dQ%d 彙總無資料(可能尚未公布)", y, q)
            continue
        for code, st in idx.items():
            hit = bulk.get(code)
            if not hit:
                continue
            row = existing.get((code, y, q))
            # 已有累計 EPS 就跳過,但淨利是後來才加抓的欄位,缺就補上(反推股數要用)
            if row and row.get("cum_eps") is not None and not overwrite:
                if hit["ni"] is None or row.get("cum_ni") is not None:
                    skipped += 1
                    continue
                try:
                    nt.upsert_eps(code, st["name"] or hit["name"], y, q,
                                  cum_ni=hit["ni"], existing=row)
                    filled += 1
                except nt.NotionError as e:
                    logger.warning("補淨利失敗(%s %dQ%d):%s", code, y, q, e)
                continue
            try:
                # existing 傳 {} 代表「已知這季還沒有列」,讓 upsert 直接建頁、省掉一次查詢
                nt.upsert_eps(code, st["name"] or hit["name"], y, q,
                              cum_eps=hit["eps"], cum_ni=hit["ni"], source="MOPS彙總回補",
                              stock_page_id=st["id"], existing=row or {})
                filled += 1
            except nt.NotionError as e:
                logger.warning("回補寫入失敗(%s %dQ%d):%s", code, y, q, e)

    for code in idx:
        try:
            recompute_quarterly(code)
        except nt.NotionError as e:
            logger.warning("重算單季失敗(%s):%s", code, e)
    return {"quarters": len(qs), "filled": filled, "skipped": skipped, "stocks": len(idx)}


# 財報公布窗口 (月, 起日, 迄日, 該窗口正在公布的季, 年份偏移)。
# 年報 3/15-4/10 公布的是「去年 Q4」、Q1 報 5/1-5/25、Q2 報 8/1-8/24、Q3 報 11/1-11/24。
# 窗口外不動作 —— 彙總表一次 1.6MB,沒必要天天抓。
# ⚠️ 這裡刻意**不**用 recent_quarters():那是以「申報期限已過」為準(Q2 要到 8/19),
#    但公司從 8/1 就陸續申報了,彙總表當天就查得到,兜底必須跟著窗口走才不會慢半個月。
_EPS_WINDOWS = (
    (3, 15, 31, 4, -1), (4, 1, 10, 4, -1),
    (5, 1, 25, 1, 0), (8, 1, 24, 2, 0), (11, 1, 24, 3, 0),
)


def _window_target(today: datetime.date):
    for m, lo, hi, q, yoff in _EPS_WINDOWS:
        if today.month == m and lo <= today.day <= hi:
            return today.year + yoff, q
    return None


def sync_latest_quarter() -> dict:
    """兜底排程用:補「最近一個已過申報期限的季」,把沒發重訊的公司補齊。"""
    qs = recent_quarters(1)
    if not qs:
        return {"filled": 0}
    res = backfill(qs=qs)
    res["target"] = f"{qs[0][0]}Q{qs[0][1]}"
    return res


def daily_backfill_job() -> dict:
    """每日排程進入點:只在財報公布窗口內跑一次兜底補漏(補沒發重訊、只默默申報的公司)。"""
    target = _window_target(datetime.date.today())
    if not target:
        return {"skipped": "非財報公布窗口"}
    res = backfill(qs=[target])
    res["target"] = f"{target[0]}Q{target[1]}"
    logger.info("EPS 兜底補漏:%s", res)
    return res


# ---------------------------------------------------------------- 增資偵測

# 年度內加權股數變動超過這個百分比,就認定該年的「累計相減」單季 EPS 不可信
_SHARE_JUMP_PCT = 5.0


def implied_shares(row: dict):
    """由「累計歸屬母公司淨利 ÷ 累計基本EPS」反推該期間的**加權平均**流通股數(億股)。
    這正是 EPS 的分母;年度內現金增資/CB 轉股/配股都會讓它逐季墊高。
    EPS 太接近 0 時商數會爆掉(分母趨近 0),視為不可用。"""
    ni, eps = row.get("cum_ni"), row.get("cum_eps")
    if ni is None or eps is None or abs(eps) < 0.1:
        return None
    return abs(ni) / abs(eps)


def share_change_warnings(rows: list[dict], years: set | None = None) -> list[str]:
    """挑出「年度內加權股數明顯變動」的年份 —— 那幾年的單季 EPS(累計相減)不能拿來估值。
    ⚠️ 只有年度**內**的變動才影響相減:Q1 單季=累計不必相減,失真只發生在 Q2~Q4。"""
    by_year: dict = {}
    for r in rows:
        s = implied_shares(r)
        if s and r.get("year") and r.get("quarter"):
            by_year.setdefault(r["year"], []).append((r["quarter"], s))
    out = []
    for y, items in sorted(by_year.items(), reverse=True):
        if years is not None and y not in years:
            continue
        if len(items) < 2:
            continue
        items.sort()
        first, last = items[0][1], items[-1][1]
        if not first:
            continue
        pct = (last - first) / first * 100
        if abs(pct) >= _SHARE_JUMP_PCT:
            out.append(f"⚠️ {y} 加權股數 {first:.2f}→{last:.2f} 億股({pct:+.0f}%),"
                       f"該年單季 EPS 僅供參考")
    return out


# ---------------------------------------------------------------- 呈現

def format_eps_table(code: str, name: str = "", limit: int = 6) -> str:
    """把某檔的 EPS 表格排成 LINE 訊息(新到舊)。"""
    all_rows = nt.list_eps(code=code)
    rows = all_rows[:limit]
    if not rows:
        return f"{code} 目前沒有 EPS 資料。財報公布後會自動抓,或打「EPS 回補」補歷史。"
    title = f"{code} {name or rows[0].get('name', '')}".strip()
    lines = [f"📊 {title} 每股盈餘", "季別｜單季｜累計"]
    # rows 已由新到舊排序,同一年份的季會連在一起 → 每個年份區塊結束時補一行小計。
    # 小計直接取該年「最大季的累計 EPS」(季報的累計本來就是年初至今),不用自己加總,
    # 這樣即使該年只顯示到 Q4 一列,小計仍是正確的全年值。
    def year_total(year: int) -> str:
        same = [r for r in rows if r["year"] == year and r["cum_eps"] is not None]
        if not same:
            return ""
        top = max(same, key=lambda r: r["quarter"] or 0)
        label = "全年" if top["quarter"] == 4 else f"至Q{top['quarter']}"
        return f"　└ {year} {label} {top['cum_eps']:.2f}"

    for i, r in enumerate(rows):
        y, q = r["year"], r["quarter"]
        cum = f"{r['cum_eps']:.2f}" if r["cum_eps"] is not None else "－"
        single = f"{r['q_eps']:.2f}" if r["q_eps"] is not None else "－"
        mark = "" if r["source"] == "重訊詳細頁" else "*"
        lines.append(f"{y}Q{q}｜{single}｜{cum}{mark}")
        is_last_of_year = i + 1 == len(rows) or rows[i + 1]["year"] != y
        if is_last_of_year:
            total = year_total(y)
            if total:
                lines.append(total)
    # 增資警示用**全部**季判斷(不只表上顯示的 6 季),否則跨年的股數變化會看不出來
    lines += share_change_warnings(all_rows, years={r["year"] for r in rows})
    lines.append("＊=彙總表回補;單季由累計相減")
    return "\n".join(lines)


# ---------------------------------------------------------------- LINE 指令

_EPS_USAGE = (
    "📊 EPS 用法\n"
    "EPS 2330 → 該檔近 6 季單季/累計每股盈餘\n"
    "EPS 台積電 → 也可用主表裡的名稱\n"
    "EPS 回補 → 用 MOPS 彙總表補齊追蹤股近 8 季(要跑 1~2 分鐘)\n"
    "\n"
    "平常不用管:追蹤股一發「董事會通過財務報告」重訊,2 小時內自動抓進表;\n"
    "沒發重訊的公司由每晚兜底補漏(只在財報公布月動作)。"
)


def is_eps_command(text: str) -> bool:
    t = (text or "").strip()
    return t.upper() == "EPS" or t.upper().startswith("EPS ") or t.upper().startswith("EPS　")


def is_backfill_command(text: str) -> bool:
    return (text or "").strip()[3:].replace("　", " ").strip() in ("回補", "補", "回補歷史")


def run_eps_query(text: str) -> str:
    """「EPS 2330」查表格;「EPS 回補」補歷史;「EPS 用法」說明。
    只打「EPS」或查不到對應個股 → 回空字串(不回覆,避免群組誤觸)。"""
    arg = (text or "").strip()[3:].replace("　", " ").strip()
    if not arg:
        return ""
    if arg in ("用法", "說明", "help", "?"):
        return _EPS_USAGE
    if arg in ("回補", "補", "回補歷史"):
        res = backfill(quarters=8)
        return (f"✅ EPS 回補完成:{res['stocks']} 檔 × 近 {res['quarters']} 季\n"
                f"新填 {res['filled']} 格、既有跳過 {res['skipped']} 格\n"
                f"打「EPS 代號」看表格。")

    code, name = "", ""
    if re.fullmatch(r"\d{3,6}", arg):
        code = arg
    for s in nt.list_stocks():
        if not s["code"]:
            continue
        label_name = s["label"].replace(s["code"], "", 1).strip()
        if (code and s["code"] == code) or (not code and arg == label_name):
            code, name = s["code"], label_name
            break
    if not code:
        return ""
    return format_eps_table(code, name)

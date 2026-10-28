"""Daily News Briefing data collector.

GitHub Actions에서 매일 실행되어 공개 데이터를 수집하고 data/latest.json 으로 저장한다.
각 소스는 독립적으로 실패할 수 있으며, 실패 내용은 status 에 기록된다.
(LLM 브리핑은 이 파일을 읽어 해설만 작성한다)
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ARCHIVE = DATA / "archive"
STATE = DATA / "state"
KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "Mozilla/5.0 (news-briefing-collector; +https://github.com/mingunpark/new_briefing)"}
TIMEOUT = 20

status: dict[str, dict] = {}


def get(url: str, attempts: int = 3, **kw) -> requests.Response:
    """429/5xx/네트워크 오류만 재시도. 400·403·404 같은 영구 오류는 바로 실패 처리."""
    headers = {**UA, **kw.pop("headers", {})}
    last = None
    for attempt in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} (no retry): {url}")
        return r
    raise RuntimeError(f"GET failed after {attempts} attempts: {url} ({last})")


def run(name: str, fn):
    """소스 하나 실행. 실패해도 전체는 계속 진행."""
    try:
        result = fn()
        status[name] = {"ok": True}
        return result
    except Exception as e:  # noqa: BLE001
        status[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        traceback.print_exc()
        return None


# ───────────────────────── 시장 지표 ─────────────────────────
MARKET_TICKERS = {
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DJIA": "^DJI",
    "VIX": "^VIX",
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
    "DXY": "DX-Y.NYB",
    "USDKRW": "KRW=X",
    "US10Y_YAHOO": "^TNX",
    "WTI": "CL=F",
    "GOLD": "GC=F",
    "BTC": "BTC-USD",
}


def market_yahoo() -> dict:
    import yfinance as yf

    out = {}
    for key, sym in MARKET_TICKERS.items():
        try:
            hist = yf.Ticker(sym).history(period="10d", interval="1d", auto_adjust=False)
            hist = hist.dropna(subset=["Close"])
            if len(hist) < 2:
                raise ValueError("insufficient rows")
            last, prev = hist.iloc[-1], hist.iloc[-2]
            close, pclose = float(last["Close"]), float(prev["Close"])
            out[key] = {
                "symbol": sym,
                "close": round(close, 4),
                "prev_close": round(pclose, 4),
                "change": round(close - pclose, 4),
                "change_pct": round((close / pclose - 1) * 100, 3),
                "as_of": hist.index[-1].strftime("%Y-%m-%d"),
                "source": f"Yahoo Finance {sym}",
            }
        except Exception as e:  # noqa: BLE001
            out[key] = {"symbol": sym, "error": f"{type(e).__name__}: {e}"[:200]}
    if all("error" in v for v in out.values()):
        raise RuntimeError("all yahoo tickers failed")
    return out


FRED_CROSS = {"SP500": "SP500", "NASDAQ": "NASDAQCOM", "DJIA": "DJIA", "USDKRW": "DEXKOUS"}


def market_fred_crosscheck() -> dict:
    """교차검증용 2차 소스 (FRED 지수·환율 시계열, 1영업일 늦게 게시될 수 있음)."""
    out = {}
    for key, series in FRED_CROSS.items():
        try:
            f = fred(series)
            out[key] = {
                "close": f["value"],
                "change_pct": round((f["value"] / f["prev_value"] - 1) * 100, 3),
                "as_of": f["as_of"],
                "source": f"FRED {series}",
            }
        except Exception as e:  # noqa: BLE001
            out[key] = {"error": f"{type(e).__name__}: {e}"[:200]}
    if all("error" in v for v in out.values()):
        raise RuntimeError("all FRED crosscheck series failed")
    return out


# ───────────────────────── 금리 / TGA ─────────────────────────
def fred(series: str) -> dict:
    r = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")
    rows = list(csv.reader(io.StringIO(r.text)))[1:]
    vals = [(d, float(v)) for d, v in rows if v not in ("", ".")]
    if len(vals) < 2:
        raise ValueError("insufficient rows")
    (d0, v0), (d1, v1) = vals[-2], vals[-1]
    return {
        "value": v1,
        "as_of": d1,
        "prev_value": v0,
        "prev_as_of": d0,
        "change_bp": round((v1 - v0) * 100, 1),
        "recent": [{"date": d, "value": v} for d, v in vals[-10:]],
        "source": f"FRED {series} (확정치, 1영업일 지연 게시)",
    }


def treasury_par_yield() -> dict:
    year = NOW.year
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )
    rows = list(csv.DictReader(io.StringIO(get(url).text)))
    if len(rows) < 2:
        raise ValueError("insufficient rows")

    def key(r):
        return datetime.strptime(r["Date"], "%m/%d/%Y")

    rows.sort(key=key)
    last, prev = rows[-1], rows[-2]
    pick = ["3 Mo", "2 Yr", "10 Yr", "30 Yr"]
    return {
        "as_of": key(last).strftime("%Y-%m-%d"),
        "prev_as_of": key(prev).strftime("%Y-%m-%d"),
        "yields": {k: float(last[k]) for k in pick if last.get(k)},
        "prev_yields": {k: float(prev[k]) for k in pick if prev.get(k)},
        "change_bp_10y": round((float(last["10 Yr"]) - float(prev["10 Yr"])) * 100, 1),
        "source": "US Treasury Daily Par Yield Curve",
    }


def tga() -> dict:
    since = (NOW - timedelta(days=21)).strftime("%Y-%m-%d")
    url = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/"
        f"operating_cash_balance?filter=record_date:gte:{since}&sort=-record_date&page[size]=200"
    )
    rows = get(url).json()["data"]
    closing = [r for r in rows if "Closing Balance" in r["account_type"] and "TGA" in r["account_type"]]

    def val(r):
        # 주의: 이 행은 close_today_bal 이 "null" 이고 실제 값은 open_today_bal 에 들어 있음 (단위: 백만 달러)
        for f in ("close_today_bal", "open_today_bal"):
            v = r.get(f)
            if v not in (None, "", "null"):
                return float(v)
        raise ValueError("no balance field")

    series = sorted({r["record_date"]: val(r) for r in closing}.items())
    if not series:
        raise ValueError("no TGA closing rows")
    latest_d, latest_v = series[-1]
    week_ago = [v for d, v in series if d <= (datetime.fromisoformat(latest_d) - timedelta(days=7)).strftime("%Y-%m-%d")]
    prev_d, prev_v = series[-2] if len(series) > 1 else (None, None)
    return {
        "record_date": latest_d,
        "balance_musd": latest_v,
        "balance_busd": round(latest_v / 1000, 1),
        "dod_change_busd": round((latest_v - prev_v) / 1000, 1) if prev_v else None,
        "wow_change_busd": round((latest_v - week_ago[-1]) / 1000, 1) if week_ago else None,
        "series_busd": [{"date": d, "value": round(v / 1000, 1)} for d, v in series],
        "source": "fiscaldata.treasury.gov DTS operating_cash_balance",
    }


# ───────────────────────── AI 트렌드 소스 ─────────────────────────
def hn() -> list:
    since = int((NOW - timedelta(hours=26)).timestamp())
    url = (
        "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=200"
        f"&numericFilters=created_at_i>{since},points>80"
    )
    hits = get(url).json()["hits"]
    hits.sort(key=lambda h: h.get("points") or 0, reverse=True)
    return [
        {
            "title": h.get("title"),
            "url": h.get("url"),
            "hn_url": f"https://news.ycombinator.com/item?id={h['objectID']}",
            "points": h.get("points"),
            "comments": h.get("num_comments"),
            "created_at": h.get("created_at"),
        }
        for h in hits[:40]
    ]


def github_trending() -> list:
    html = get("https://github.com/trending?since=daily").text
    out = []
    for art in re.findall(r"<article class=\"Box-row\">(.*?)</article>", html, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', art, re.S)
        if not m:
            continue
        desc = re.search(r"<p[^>]*>(.*?)</p>", art, re.S)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        today = re.search(r"([\d,]+)\s+stars? today", art)
        total = re.search(r'href="/[^"]+/stargazers"[^>]*>.*?([\d,]+)\s*</a>', art, re.S)
        out.append(
            {
                "repo": m.group(1).strip(),
                "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
                "language": lang.group(1).strip() if lang else None,
                "stars_today": int(today.group(1).replace(",", "")) if today else None,
                "stars_total": int(total.group(1).replace(",", "")) if total else None,
            }
        )
    if not out:
        raise ValueError("trending page parsed 0 repos (markup changed?)")
    return out


def gh_api(path: str, params: dict) -> dict:
    import os

    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return get(f"https://api.github.com{path}", params=params, headers=headers).json()


def github_new_repos() -> list:
    """최근 45일 내 생성 + star 순. star 총량 기준으로 거르지 않는다 (REA 같은 급성장 레포 포착용)."""
    since = (NOW - timedelta(days=45)).strftime("%Y-%m-%d")
    data = gh_api("/search/repositories", {"q": f"created:>{since} stars:>300", "sort": "stars", "order": "desc", "per_page": 50})
    return [
        {
            "repo": it["full_name"],
            "description": it.get("description"),
            "stars_total": it["stargazers_count"],
            "forks": it.get("forks_count"),
            "open_issues": it.get("open_issues_count"),
            "created_at": it["created_at"],
            "language": it.get("language"),
            "license": (it.get("license") or {}).get("spdx_id"),
            "topics": it.get("topics", [])[:8],
        }
        for it in data.get("items", [])
    ]


def star_velocity(trending: list | None, new_repos: list | None) -> list:
    """저장소에 남겨 둔 어제까지의 star 스냅샷과 비교해 1일/7일 증가량 계산."""
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "github_stars.json"
    hist: dict[str, dict[str, int]] = json.loads(path.read_text()) if path.exists() else {}
    today = NOW.astimezone(KST).strftime("%Y-%m-%d")
    for src in (trending or []), (new_repos or []):
        for r in src:
            if r.get("stars_total"):
                hist.setdefault(r["repo"], {})[today] = r["stars_total"]
    # 30일 지난 기록은 정리
    cutoff = (NOW - timedelta(days=30)).astimezone(KST).strftime("%Y-%m-%d")
    hist = {k: {d: s for d, s in v.items() if d >= cutoff} for k, v in hist.items()}
    hist = {k: v for k, v in hist.items() if v}
    path.write_text(json.dumps(hist, ensure_ascii=False, indent=0, sort_keys=True))

    out = []
    for repo, snaps in hist.items():
        days = sorted(snaps)
        if today not in snaps or len(days) < 2:
            continue
        prev = days[-2]
        wk = [d for d in days if d <= (datetime.fromisoformat(today) - timedelta(days=7)).strftime("%Y-%m-%d")]
        out.append(
            {
                "repo": repo,
                "stars_total": snaps[today],
                "gain_since_prev": snaps[today] - snaps[prev],
                "prev_snapshot": prev,
                "gain_7d": snaps[today] - snaps[wk[-1]] if wk else None,
            }
        )
    out.sort(key=lambda x: x["gain_since_prev"], reverse=True)
    return out[:30]


def reddit() -> dict:
    subs = ["LocalLLaMA", "MachineLearning", "artificial", "AI_Agents", "singularity"]
    out = {}
    for s in subs:
        try:
            d = get(f"https://www.reddit.com/r/{s}/top.json?t=day&limit=10", attempts=1, headers={"Accept": "application/json"}).json()
            out[s] = [
                {
                    "title": c["data"]["title"],
                    "score": c["data"]["score"],
                    "comments": c["data"]["num_comments"],
                    "url": "https://www.reddit.com" + c["data"]["permalink"],
                }
                for c in d["data"]["children"]
            ]
        except Exception as e:  # noqa: BLE001
            out[s] = {"error": f"{type(e).__name__}: {e}"[:200]}
    if all(isinstance(v, dict) for v in out.values()):
        raise RuntimeError("reddit blocked for all subs (datacenter IP 차단 가능성)")
    return out


def hf_trending() -> dict:
    out = {}
    for t in ("model", "dataset", "space"):
        items = get(f"https://huggingface.co/api/trending?type={t}").json().get("recentlyTrending", [])
        rows = []
        for it in items:
            d = it.get("repoData", {})
            rows.append(
                {
                    "id": d.get("id"),
                    "url": f"https://huggingface.co/{'spaces/' if t == 'space' else 'datasets/' if t == 'dataset' else ''}{d.get('id')}",
                    "likes": d.get("likes"),
                    "downloads": d.get("downloads"),
                    "pipeline_tag": d.get("pipeline_tag"),
                    "num_parameters": d.get("numParameters"),
                    "created_at": d.get("createdAt"),
                    "last_modified": d.get("lastModified"),
                    "runtime_stage": (d.get("runtime") or {}).get("stage"),
                    "ai_category": d.get("ai_category"),
                }
            )
        out[t] = rows[:30]
    return out


def hf_papers() -> list:
    d = get("https://huggingface.co/api/daily_papers?limit=30").json()
    return [
        {
            "title": p.get("paper", {}).get("title") or p.get("title"),
            "arxiv_id": p.get("paper", {}).get("id"),
            "upvotes": p.get("paper", {}).get("upvotes"),
            "published_at": p.get("publishedAt"),
            "url": f"https://huggingface.co/papers/{p.get('paper', {}).get('id')}",
        }
        for p in d
    ]


# ───────────────────────── main ─────────────────────────
def main():
    DATA.mkdir(exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    trending = run("github_trending", github_trending)
    new_repos = run("github_new_repos", github_new_repos)

    result = {
        "generated_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_kst": NOW.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
        "notes": {
            "tga": "단위 busd=십억 달러. record_date 는 DTS 기준일(T+1 영업일 게시).",
            "fred_dgs10": "FRED 는 1영업일 늦게 게시되므로 최신 거래일 값은 treasury_par_yield 를 우선 사용.",
            "usdkrw": "Yahoo KRW=X 는 글로벌 호가 기준이며 서울 외환시장 종가와 다를 수 있음.",
            "fake_stars": "star 대비 forks 가 극단적으로 적으면(예: 1% 미만) star 조작 가능성 의심.",
            "intraday": "as_of 가 오늘(KST) 날짜인 국내·환율 값은 장중 값일 수 있음.",
        },
        "market": {
            "yahoo": run("market_yahoo", market_yahoo),
            "fred_crosscheck": run("market_fred_crosscheck", market_fred_crosscheck),
        },
        "rates": {
            "treasury_par_yield": run("treasury_par_yield", treasury_par_yield),
            "fred_dgs10": run("fred_dgs10", lambda: fred("DGS10")),
            "fred_dgs2": run("fred_dgs2", lambda: fred("DGS2")),
        },
        "tga": run("tga", tga),
        "ai": {
            "hn_top_24h": run("hn", hn),
            "github_trending_daily": trending,
            "github_new_repos_45d": new_repos,
            "github_star_velocity": run("github_star_velocity", lambda: star_velocity(trending, new_repos)),
            "reddit_top_day": run("reddit", reddit),
            "hf_trending": run("hf_trending", hf_trending),
            "hf_daily_papers": run("hf_papers", hf_papers),
        },
    }
    result["status"] = status
    result["failed_sources"] = [k for k, v in status.items() if not v["ok"]]

    text = json.dumps(result, ensure_ascii=False, indent=1)
    (DATA / "latest.json").write_text(text)
    (ARCHIVE / f"{NOW.astimezone(KST).strftime('%Y-%m-%d')}.json").write_text(text)
    # 60일 지난 아카이브 정리
    cutoff = (NOW - timedelta(days=60)).astimezone(KST).strftime("%Y-%m-%d")
    for f in ARCHIVE.glob("*.json"):
        if f.stem < cutoff:
            f.unlink()
    print(json.dumps(status, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

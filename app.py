"""
Canada Economic Monitor
=======================

A self-refreshing Streamlit dashboard that tracks the Canadian economy using
primary sources only:

* Statistics Canada - Web Data Service (WDS) REST API
* Bank of Canada    - Valet API

Two ways to run the same file:

    streamlit run app.py          # interactive dashboard (+ built-in scheduler)
    python app.py --refresh       # one refresh + verification, writes a log
    python app.py --verify        # re-check cached data against reference values
    python app.py --loop          # headless scheduler (refresh every N hours)

Data are cached in ./data (observations.csv, refresh_meta.json, refresh.log).
Python 3.12+.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CEM_DATA_DIR", APP_DIR / "data"))
OBS_FILE = DATA_DIR / "observations.csv"
META_FILE = DATA_DIR / "refresh_meta.json"
LOG_FILE = DATA_DIR / "refresh.log"
LOCK_FILE = DATA_DIR / ".refresh.lock"

REFRESH_HOURS = float(os.environ.get("CEM_REFRESH_HOURS", "6"))   # schedule
RETRY_MINUTES = float(os.environ.get("CEM_RETRY_MINUTES", "30"))  # after a failure
SCHEDULER_TICK_SECONDS = 300            # how often the scheduler wakes up
PAGE_POLL = timedelta(minutes=5)        # how often an open page re-reads the cache
FETCH_START = "2010-01-01"              # history retrieved (y/y needs 12 extra months)
DEFAULT_VIEW_START_YEAR = 2015          # default chart window start
HTTP_TIMEOUT = 45
HTTP_RETRIES = 3

ET = ZoneInfo("America/Toronto")        # StatCan and BoC publish on Eastern time

VALET_URL = "https://www.bankofcanada.ca/valet/observations/{codes}/json"
WDS_URL = ("https://www150.statcan.gc.ca/t1/wds/rest/"
           "getDataFromVectorByReferencePeriodRange")

# Categorical slots 1-3 of the validated reference palette, plus neutral ink.
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"
NEUTRAL = "#8a8984"
INK, INK_2 = "#0b0b0b", "#52514e"


# --------------------------------------------------------------------------- #
# Series catalogue (each series = one primary-source time series)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Series:
    key: str
    source: str          # "statcan" | "boc"
    code: str            # StatCan vector number (digits) or Valet series code
    name: str
    units: str
    frequency: str
    adjustment: str
    source_label: str
    url: str
    revisable: bool = False   # True if the publisher routinely revises history


SERIES: dict[str, Series] = {s.key: s for s in [
    Series("gdp", "statcan", "65201210",
           "Real GDP at basic prices, all industries",
           "Millions of chained (2017) dollars, annualized",
           "Monthly", "Seasonally adjusted at annual rates",
           "Statistics Canada, Table 36-10-0434-01 (vector v65201210)",
           "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3610043401",
           revisable=True),
    Series("cpi", "statcan", "41690973",
           "Consumer Price Index, all-items",
           "Index, 2002=100", "Monthly", "Not seasonally adjusted",
           "Statistics Canada, Table 18-10-0004-01 (vector v41690973)",
           "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1810000401"),
    Series("cpi_trim", "statcan", "108785715",
           "CPI-trim (Bank of Canada preferred core measure)",
           "Percent change from 12 months earlier", "Monthly",
           "Seasonally adjusted",
           "Statistics Canada, Table 18-10-0256-01 (vector v108785715)",
           "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1810025601",
           revisable=True),
    Series("cpi_median", "statcan", "108785714",
           "CPI-median (Bank of Canada preferred core measure)",
           "Percent change from 12 months earlier", "Monthly",
           "Seasonally adjusted",
           "Statistics Canada, Table 18-10-0256-01 (vector v108785714)",
           "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1810025601",
           revisable=True),
    Series("unemp", "statcan", "2062815",
           "Unemployment rate, 15 years and over",
           "Percent of labour force", "Monthly", "Seasonally adjusted",
           "Statistics Canada, Labour Force Survey, Table 14-10-0287-01 "
           "(vector v2062815)",
           "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1410028701",
           revisable=True),
    Series("policy", "boc", "V39079",
           "Target for the overnight rate (policy rate)",
           "Percent", "Daily (business days)", "Not applicable",
           "Bank of Canada, Valet series V39079",
           "https://www.bankofcanada.ca/valet/observations/V39079/json?recent=10"),
    Series("gov2y", "boc", "BD.CDN.2YR.DQ.YLD",
           "Government of Canada 2-year benchmark bond yield",
           "Percent", "Daily (business days)", "Not applicable",
           "Bank of Canada, Valet series BD.CDN.2YR.DQ.YLD",
           "https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/"),
    Series("gov10y", "boc", "BD.CDN.10YR.DQ.YLD",
           "Government of Canada 10-year benchmark bond yield",
           "Percent", "Daily (business days)", "Not applicable",
           "Bank of Canada, Valet series BD.CDN.10YR.DQ.YLD",
           "https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/"),
    Series("usdcad", "boc", "FXUSDCAD",
           "US dollar to Canadian dollar daily average exchange rate",
           "Canadian dollars per 1 US dollar (USD/CAD)",
           "Daily (business days)", "Not applicable",
           "Bank of Canada, Valet series FXUSDCAD",
           "https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates/"),
]}

# --------------------------------------------------------------------------- #
# Reference observations, checked against the original publications by hand
# (Nov 22, 2026) and re-checked automatically on every refresh.
#   kind="level": raw series value; kind="yoy": derived 12-month % change.
# --------------------------------------------------------------------------- #
REFERENCE_CHECKS = [
    dict(series="gdp", date="2026-08-01", value=2371635, tol=0.5, kind="level",
         source="StatCan Table 36-10-0434-01, released 2026-10-30 "
                "(The Daily, 'GDP by industry, August 2026')",
         url="https://www150.statcan.gc.ca/n1/daily-quotidien/261030/dq261030a-eng.htm"),
    dict(series="cpi", date="2026-10-01", value=170.4, tol=0.05, kind="level",
         source="Bank of Canada CPI page (reproduces StatCan v41690973), "
                "October 2026 CPI released 2026-11-16",
         url="https://www.bankofcanada.ca/rates/price-indexes/cpi/"),
    dict(series="cpi", date="2026-10-01", value=3.1, tol=0.05, kind="yoy",
         source="StatCan, 'Consumer Price Index, October 2026' (+3.1% y/y); "
                "checks our y/y transformation",
         url="https://www.bankofcanada.ca/rates/price-indexes/cpi/"),
    dict(series="cpi_trim", date="2026-10-01", value=1.8, tol=0.05, kind="level",
         source="Bank of Canada Daily Digest / CPI page, October 2026",
         url="https://www.bankofcanada.ca/rates/daily-digest"),
    dict(series="cpi_median", date="2026-10-01", value=2.0, tol=0.05, kind="level",
         source="Bank of Canada Daily Digest / CPI page, October 2026",
         url="https://www.bankofcanada.ca/rates/daily-digest"),
    dict(series="unemp", date="2026-10-01", value=6.7, tol=0.05, kind="level",
         source="StatCan, 'Labour Force Survey, October 2026', released 2026-11-06",
         url="https://www150.statcan.gc.ca/daily-quotidien/261106/dq261106a-eng.htm"),
    dict(series="policy", date="2026-11-19", value=2.50, tol=0.001, kind="level",
         source="Bank of Canada Daily Digest (target for the overnight rate)",
         url="https://www.bankofcanada.ca/rates/daily-digest"),
    dict(series="gov2y", date="2026-11-19", value=3.30, tol=0.005, kind="level",
         source="Bank of Canada, Selected bond yields page",
         url="https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/"),
    dict(series="gov10y", date="2026-11-19", value=3.81, tol=0.005, kind="level",
         source="Bank of Canada, Selected bond yields page",
         url="https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/"),
    dict(series="usdcad", date="2026-11-20", value=1.4325, tol=0.00005, kind="level",
         source="Bank of Canada, Daily exchange rates page (US dollar row)",
         url="https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates/"),
]

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
log = logging.getLogger("cem")


def setup_logging(to_stdout: bool = True) -> None:
    if log.handlers:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S%z")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


# --------------------------------------------------------------------------- #
# HTTP + source-specific fetchers
# --------------------------------------------------------------------------- #
_session = requests.Session()
_session.headers.update({"User-Agent": "canada-economic-monitor/1.0 (+python-requests)",
                         "Accept": "application/json"})


def _http_get(url: str, params: dict | None = None) -> object:
    """GET a JSON document with retries and exponential back-off."""
    last_exc: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            log.warning("GET %s failed (attempt %d/%d): %s",
                        url, attempt, HTTP_RETRIES, exc)
            if attempt < HTTP_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRIES} attempts: {last_exc}")


def fetch_statcan(vectors: list[str], start: str, end: str) -> tuple[dict, dict]:
    """StatCan WDS: getDataFromVectorByReferencePeriodRange (one request)."""
    params = {"vectorIds": ",".join(f'"{v}"' for v in vectors),
              "startRefPeriod": start, "endReferencePeriod": end}
    payload = _http_get(WDS_URL, params)
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected WDS response: {str(payload)[:200]}")
    out, release = {}, {}
    for item in payload:
        obj = item.get("object") or {}
        vid = str(obj.get("vectorId", ""))
        if item.get("status") != "SUCCESS" or not vid:
            log.warning("WDS returned status=%s for %s", item.get("status"), obj)
            continue
        rows = [(p["refPer"], p.get("value")) for p in obj.get("vectorDataPoint", [])]
        df = pd.DataFrame(rows, columns=["date", "value"]).dropna()
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        out[vid] = df.dropna().sort_values("date")
        times = [p.get("releaseTime") for p in obj.get("vectorDataPoint", [])
                 if p.get("releaseTime")]
        release[vid] = max(times) if times else None
    return out, release


def fetch_valet(codes: list[str], start: str) -> dict:
    """Bank of Canada Valet: several series in one request."""
    payload = _http_get(VALET_URL.format(codes=",".join(codes)),
                        {"start_date": start})
    obs = payload.get("observations", []) if isinstance(payload, dict) else []
    out = {}
    for code in codes:
        rows = []
        for o in obs:
            cell = o.get(code)
            if isinstance(cell, dict) and cell.get("v") not in (None, ""):
                rows.append((o["d"], cell["v"]))
        df = pd.DataFrame(rows, columns=["date", "value"])
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        out[code] = df.dropna().sort_values("date")
    return out


# --------------------------------------------------------------------------- #
# Cache I/O (atomic writes so the dashboard never reads a half-written file)
# --------------------------------------------------------------------------- #
def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_observations() -> pd.DataFrame:
    if not OBS_FILE.exists():
        return pd.DataFrame(columns=["series", "date", "value"])
    df = pd.read_csv(OBS_FILE, parse_dates=["date"])
    return df


def load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _acquire_lock(max_age_s: int = 900) -> bool:
    """Cross-process lock so cron, CLI and the app never refresh concurrently."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime > max_age_s:
            LOCK_FILE.unlink(missing_ok=True)          # stale lock
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Transformations
# --------------------------------------------------------------------------- #
def series_frame(obs: pd.DataFrame, key: str) -> pd.DataFrame:
    df = obs[obs["series"] == key][["date", "value"]].sort_values("date")
    return df.reset_index(drop=True)


def yoy_pct(df: pd.DataFrame) -> pd.DataFrame:
    """12-month % change for a monthly series: 100*(x_t / x_{t-12} - 1).

    Matched on calendar month (not row offset) so a gap can't misalign it.
    """
    s = df.set_index("date")["value"]
    prev = s.copy()
    prev.index = prev.index + pd.DateOffset(years=1)
    out = (100 * (s / prev.reindex(s.index) - 1)).dropna()
    return out.rename("value").reset_index()


def mom_pct(df: pd.DataFrame) -> pd.DataFrame:
    s = df.set_index("date")["value"]
    prev = s.copy()
    prev.index = prev.index + pd.DateOffset(months=1)
    out = (100 * (s / prev.reindex(s.index) - 1)).dropna()
    return out.rename("value").reset_index()


# --------------------------------------------------------------------------- #
# Verification against reference observations
# --------------------------------------------------------------------------- #
def run_verification(obs: pd.DataFrame) -> list[dict]:
    results = []
    for chk in REFERENCE_CHECKS:
        df = series_frame(obs, chk["series"])
        if chk["kind"] == "yoy":
            df = yoy_pct(df)
            df["value"] = df["value"].round(1)   # StatCan publishes 1 decimal
        hit = df[df["date"] == pd.Timestamp(chk["date"])]
        if hit.empty:
            status, got = "NOT FOUND", None
        else:
            got = float(hit["value"].iloc[0])
            if abs(got - chk["value"]) <= chk["tol"]:
                status = "MATCH"
            elif SERIES[chk["series"]].revisable:
                status = "DIFFERS (likely revised)"
            else:
                status = "MISMATCH"
        results.append({**chk, "observed": got, "status": status})
    return results


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
def refresh(trigger: str = "manual") -> dict:
    """Fetch every series, merge with cache, verify, persist. Returns meta."""
    setup_logging(to_stdout=(trigger == "cli"))
    if not _acquire_lock():
        log.info("Refresh skipped (%s): another refresh is in progress", trigger)
        return load_meta()

    try:
        started = _utcnow()
        meta = load_meta()
        meta["last_attempt_utc"] = started.isoformat()
        log.info("=== Refresh started (trigger=%s) ===", trigger)

        old = load_observations()
        frames: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        release_times: dict[str, str | None] = {}
        end = (started + timedelta(days=31)).strftime("%Y-%m-%d")

        # Statistics Canada (one request for all vectors)
        sc = [s for s in SERIES.values() if s.source == "statcan"]
        try:
            data, rel = fetch_statcan([s.code for s in sc], FETCH_START, end)
            for s in sc:
                if s.code in data and not data[s.code].empty:
                    frames[s.key] = data[s.code]
                    release_times[s.key] = rel.get(s.code)
                else:
                    errors[s.key] = "no data returned by StatCan WDS"
        except Exception as exc:                               # noqa: BLE001
            for s in sc:
                errors[s.key] = f"StatCan WDS error: {exc}"

        # Bank of Canada (one request for all Valet series)
        bc = [s for s in SERIES.values() if s.source == "boc"]
        try:
            data = fetch_valet([s.code for s in bc], FETCH_START)
            for s in bc:
                if not data[s.code].empty:
                    frames[s.key] = data[s.code]
                else:
                    errors[s.key] = "no data returned by Valet"
        except Exception as exc:                               # noqa: BLE001
            for s in bc:
                errors[s.key] = f"Bank of Canada Valet error: {exc}"

        # Merge: failed series keep their last good values from the cache
        parts, per_series = [], meta.get("series", {})
        for key, s in SERIES.items():
            prev = series_frame(old, key)
            if key in frames:
                new = frames[key]
                prev_last = prev["date"].max() if not prev.empty else None
                new_last = new["date"].max()
                if prev.empty:
                    changed, added = 0, int(len(new))
                else:
                    merged = prev.merge(new, on="date", how="outer",
                                        suffixes=("_old", "_new"))
                    changed = int(((merged["value_old"] - merged["value_new"])
                                   .abs() > 1e-9).sum())
                    added = int(merged["value_old"].isna().sum())
                last_val = float(new.loc[new["date"] == new_last, "value"].iloc[0])
                per_series[key] = {
                    "rows": int(len(new)),
                    "first_obs": new["date"].min().strftime("%Y-%m-%d"),
                    "latest_obs": new_last.strftime("%Y-%m-%d"),
                    "latest_value": last_val,
                    "release_time": release_times.get(key),
                    "fetched_utc": started.isoformat(),
                    "status": "ok",
                }
                if prev_last is None:
                    note = "initial load"
                elif new_last > prev_last:
                    note = f"NEW observation(s) since {prev_last:%Y-%m-%d}"
                elif changed or added:
                    note = "history revised"
                else:
                    note = "unchanged (no new release since last refresh)"
                log.info("  OK   %-10s %-20s rows=%-5d latest=%s value=%s "
                         "| +%d new, %d revised | %s",
                         key, s.code, len(new), new_last.strftime("%Y-%m-%d"),
                         f"{last_val:.10g}", added, changed, note)
                parts.append(new.assign(series=key))
            else:
                log.error("  FAIL %-10s %-20s %s", key, s.code, errors[key])
                if not prev.empty:
                    parts.append(prev.assign(series=key))
                per_series.setdefault(key, {})
                per_series[key].update(status="stale", error=errors[key])

        if not frames:
            meta["last_status"] = "failed"
            meta["errors"] = errors
            _atomic_write_text(META_FILE, json.dumps(meta, indent=2))
            log.error("=== Refresh FAILED: no series retrieved; cache untouched ===")
            return meta

        obs = pd.concat(parts, ignore_index=True)[["series", "date", "value"]]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = OBS_FILE.with_suffix(".csv.tmp")
        obs.to_csv(tmp, index=False, date_format="%Y-%m-%d")
        os.replace(tmp, OBS_FILE)

        verification = run_verification(obs)
        for v in verification:
            log.info("  CHECK %-10s %s %-5s ref=%-10.10g got=%-10s %s",
                     v["series"], v["date"], v["kind"], v["value"],
                     "n/a" if v["observed"] is None else f"{v['observed']:.10g}",
                     v["status"])

        finished = _utcnow()
        status = "success" if not errors else "partial"
        meta.update(series=per_series, errors=errors, verification=verification,
                    last_status=status, last_finished_utc=finished.isoformat())
        if status == "success":
            meta["last_success_utc"] = finished.isoformat()
        _atomic_write_text(META_FILE, json.dumps(meta, indent=2, default=str))
        n_match = sum(v["status"] == "MATCH" for v in verification)
        log.info("=== Refresh %s in %ds: %d/%d series OK, %d/%d checks MATCH ===",
                 status.upper(), (finished - started).seconds,
                 len(frames), len(SERIES), n_match, len(verification))
        return meta
    finally:
        _release_lock()


def is_due(meta: dict) -> bool:
    """Due if never succeeded, success is older than the schedule, or a
    failed/partial attempt is older than the retry interval."""
    now = _utcnow()
    last_ok = meta.get("last_success_utc")
    if not last_ok:
        last_try = meta.get("last_attempt_utc")
        return (not last_try or
                now - datetime.fromisoformat(last_try) > timedelta(minutes=RETRY_MINUTES))
    if now - datetime.fromisoformat(last_ok) > timedelta(hours=REFRESH_HOURS):
        last_try = meta.get("last_attempt_utc", last_ok)
        return (meta.get("last_status") == "success" or
                now - datetime.fromisoformat(last_try) > timedelta(minutes=RETRY_MINUTES))
    return False


def scheduler_loop(trigger: str = "scheduler") -> None:
    while True:
        try:
            if is_due(load_meta()):
                refresh(trigger)
        except Exception:                                      # noqa: BLE001
            log.exception("Scheduled refresh crashed")
        time.sleep(SCHEDULER_TICK_SECONDS)


# --------------------------------------------------------------------------- #
# Dashboard (Streamlit)
# --------------------------------------------------------------------------- #
def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "never"
    t = datetime.fromisoformat(iso)
    return f"{t.astimezone(ET):%Y-%m-%d %H:%M %Z} ({t:%H:%M} UTC)"


def _period_label(d: pd.Timestamp, frequency: str) -> str:
    return f"{d:%B %Y}" if frequency == "Monthly" else f"{d:%Y-%m-%d}"


def run_dashboard() -> None:
    import plotly.graph_objects as go
    import streamlit as st

    st.set_page_config(page_title="Canada Economic Monitor", layout="wide",
                       page_icon=":material/monitoring:")
    setup_logging(to_stdout=False)

    @st.cache_resource
    def _start_scheduler() -> threading.Thread:
        th = threading.Thread(target=scheduler_loop, args=("app-scheduler",),
                              daemon=True, name="cem-scheduler")
        th.start()
        return th

    @st.cache_data(show_spinner=False)
    def _load(obs_mtime: float, meta_mtime: float):
        return load_observations(), load_meta()

    # First run (or long-idle app): refresh synchronously so the page has data.
    if not OBS_FILE.exists() or is_due(load_meta()):
        with st.spinner("Retrieving the latest data from Statistics Canada and "
                        "the Bank of Canada..."):
            refresh("app-startup")
    _start_scheduler()

    # ---------------- Sidebar (outside the auto-updating fragment) ----------
    with st.sidebar:
        st.header("Settings")
        indicator_names = list(INDICATORS)
        chosen = st.multiselect("Indicators to display", indicator_names,
                                default=indicator_names)
        this_year = datetime.now().year
        start_year = st.slider("Chart start year", 2011, this_year - 1,
                               DEFAULT_VIEW_START_YEAR)
        fx_invert = st.toggle("Show exchange rate as USD per CAD", value=False)
        st.divider()
        if st.button("Refresh now", type="primary", width="stretch"):
            with st.spinner("Refreshing..."):
                refresh("manual-button")
            st.cache_data.clear()
        st.caption(f"Automatic refresh every {REFRESH_HOURS:g} h "
                   f"(retry after {RETRY_MINUTES:g} min on failure). "
                   f"Open pages re-read the cache every "
                   f"{int(PAGE_POLL.total_seconds() // 60)} min.")

    @st.fragment(run_every=PAGE_POLL)
    def body(chosen, start_year, fx_invert):
        obs_m = OBS_FILE.stat().st_mtime if OBS_FILE.exists() else 0.0
        meta_m = META_FILE.stat().st_mtime if META_FILE.exists() else 0.0
        obs, meta = _load(obs_m, meta_m)

        st.title("Canada Economic Monitor")
        st.markdown("How is Canada's economy performing? Output, prices, jobs, "
                    "monetary conditions and the currency, straight from "
                    "Statistics Canada and the Bank of Canada.")

        # Refresh status - deliberately separate from observation dates
        c1, c2, c3 = st.columns(3)
        c1.metric("Last successful refresh", _fmt_ts(meta.get("last_success_utc")))
        next_due = (datetime.fromisoformat(meta["last_success_utc"])
                    + timedelta(hours=REFRESH_HOURS)).isoformat() \
            if meta.get("last_success_utc") else None
        c2.metric("Next scheduled refresh", _fmt_ts(next_due))
        status = meta.get("last_status", "n/a")
        c3.metric("Last attempt", f"{status.upper()}",
                  help=f"Attempted {_fmt_ts(meta.get('last_attempt_utc'))}")
        if meta.get("errors"):
            st.warning("Some series could not be refreshed and show their last "
                       "cached values: " + "; ".join(
                           f"**{k}**: {v}" for k, v in meta["errors"].items()))
        st.caption("Refresh time = when this dashboard last pulled data. "
                   "Each chart's *latest observation* = the most recent period "
                   "the publisher has released. They differ by design.")

        if obs.empty:
            st.error("No data cached yet. Check your internet connection and "
                     "press **Refresh now**.")
            return

        tab_charts, tab_data = st.tabs(["Dashboard", "Data, sources & verification"])
        with tab_charts:
            view_start = pd.Timestamp(f"{start_year}-01-01")
            for name in chosen:
                INDICATORS[name](st, go, obs, view_start, fx_invert)
                st.divider()
        with tab_data:
            _render_data_tab(st, obs, meta)

    body(chosen, start_year, fx_invert)


# ------------------------------ chart helpers ------------------------------ #
def _base_layout(fig, y_title: str, height: int = 380):
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=10, b=10),
        hovermode="x unified", plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    font=dict(color=INK_2)),
        font=dict(color=INK_2, size=13),
    )
    fig.update_xaxes(showgrid=False, linecolor="#d6d5d0", ticks="outside",
                     tickcolor="#d6d5d0")
    fig.update_yaxes(title_text=y_title, gridcolor="rgba(0,0,0,0.07)",
                     zeroline=True, zerolinecolor="#b9b8b2")
    return fig


def _line(go, df, name, color, fmt, step=False, width=2):
    return go.Scatter(x=df["date"], y=df["value"], name=name, mode="lines",
                      line=dict(color=color, width=width,
                                shape="hv" if step else "linear"),
                      hovertemplate=f"%{{y:{fmt}}}<extra>{name}</extra>")


def _meta_block(st, rows: list[tuple[str, str]]):
    md = "\n".join(f"- **{k}:** {v}" for k, v in rows)
    st.markdown(md)


def _latest(df: pd.DataFrame):
    return df["date"].iloc[-1], float(df["value"].iloc[-1])


def chart_gdp(st, go, obs, view_start, fx_invert):
    s = SERIES["gdp"]
    lvl = series_frame(obs, "gdp")
    if lvl.empty:
        return st.info("GDP data unavailable.")
    yoy, mom = yoy_pct(lvl), mom_pct(lvl)
    d, v = _latest(lvl)
    st.subheader("1. Real GDP growth")
    k1, k2, k3 = st.columns(3)
    k1.metric(f"Monthly change, {d:%b %Y}", f"{mom['value'].iloc[-1]:+.1f}%")
    k2.metric(f"Change from a year earlier, {d:%b %Y}", f"{yoy['value'].iloc[-1]:+.1f}%")
    k3.metric("Level (annualized)", f"${v / 1e6:,.3f} trillion")
    m, y = mom[mom.date >= view_start], yoy[yoy.date >= view_start]
    fig = go.Figure()
    fig.add_bar(x=m["date"], y=m["value"], name="Month-over-month %",
                marker=dict(color=C1, line=dict(width=0)),
                hovertemplate="%{y:+.2f}%<extra>Month-over-month</extra>")
    fig.add_trace(_line(go, y, "Year-over-year %", C2, "+.2f"))
    _base_layout(fig, "Percent change")
    fig.update_layout(bargap=0.15)
    # COVID-era swings (-11% m/m) would flatten everything else
    lo, hi = min(m["value"].min(), y["value"].min()), max(m["value"].max(), y["value"].max())
    if view_start.year <= 2021 and (lo < -4 or hi > 6):
        fig.update_yaxes(range=[-4, 6])
        st.caption("Y-axis clipped to −4%…+6% so recent movements are readable; "
                   "2020–21 pandemic swings extend beyond it (hover for values).")
    st.plotly_chart(fig, width="stretch")
    _meta_block(st, [
        ("Series", f"{s.name}"),
        ("Units", f"{s.units}; plotted as % change"),
        ("Frequency / adjustment", f"{s.frequency}; {s.adjustment}"),
        ("Source", f"[{s.source_label}]({s.url})"),
        ("Latest observation", _period_label(d, s.frequency)),
        ("Transformation", "Month-over-month % = 100×(GDPₜ/GDPₜ₋₁ − 1). "
         "Year-over-year % = 100×(GDPₜ/GDPₜ₋₁₂ − 1). Monthly m/m changes are not "
         "annualized. Monthly GDP by industry differs slightly from the quarterly "
         "expenditure-based GDP headline."),
    ])


def chart_cpi(st, go, obs, view_start, fx_invert):
    cpi = series_frame(obs, "cpi")
    if cpi.empty:
        return st.info("CPI data unavailable.")
    yoy = yoy_pct(cpi)
    trim, med = series_frame(obs, "cpi_trim"), series_frame(obs, "cpi_median")
    d, idx = _latest(cpi)
    st.subheader("2. Inflation: headline and core CPI")
    k1, k2, k3 = st.columns(3)
    k1.metric(f"Headline CPI, {d:%b %Y}", f"{yoy['value'].iloc[-1]:.1f}%",
              help=f"Index level {idx:.1f} (2002=100)")
    if not trim.empty:
        k2.metric(f"CPI-trim, {trim['date'].iloc[-1]:%b %Y}", f"{trim['value'].iloc[-1]:.1f}%")
    if not med.empty:
        k3.metric(f"CPI-median, {med['date'].iloc[-1]:%b %Y}", f"{med['value'].iloc[-1]:.1f}%")
    fig = go.Figure()
    fig.add_hrect(y0=1, y1=3, fillcolor=NEUTRAL, opacity=0.12, line_width=0)
    fig.add_hline(y=2, line=dict(color=NEUTRAL, width=1, dash="dot"),
                  annotation_text="2% target (1–3% range shaded)",
                  annotation_font_color=INK_2, annotation_position="top left")
    fig.add_trace(_line(go, yoy[yoy.date >= view_start], "Headline CPI", C1, ".1f"))
    if not trim.empty:
        fig.add_trace(_line(go, trim[trim.date >= view_start], "CPI-trim", C2, ".1f"))
    if not med.empty:
        fig.add_trace(_line(go, med[med.date >= view_start], "CPI-median", C3, ".1f"))
    _base_layout(fig, "% change from 12 months earlier")
    st.plotly_chart(fig, width="stretch")
    a, t = SERIES["cpi"], SERIES["cpi_trim"]
    _meta_block(st, [
        ("Units", "Percent change from the same month a year earlier"),
        ("Frequency / adjustment", "Monthly; headline CPI not seasonally adjusted, "
         "CPI-trim and CPI-median seasonally adjusted"),
        ("Source", f"[{a.source_label}]({a.url}); [{t.source_label}; "
         f"CPI-median v108785714]({t.url})"),
        ("Latest observation", _period_label(d, "Monthly")),
        ("Transformation", "Headline: computed here from the all-items index "
         "(2002=100) as 100×(CPIₜ/CPIₜ₋₁₂ − 1), rounded to 1 decimal as StatCan "
         "publishes. CPI-trim/median are published by StatCan already as 12-month "
         "% changes (no transformation)."),
    ])


def chart_unemp(st, go, obs, view_start, fx_invert):
    s = SERIES["unemp"]
    df = series_frame(obs, "unemp")
    if df.empty:
        return st.info("Unemployment data unavailable.")
    d, v = _latest(df)
    prev = float(df["value"].iloc[-2])
    yr = df[df.date == d - pd.DateOffset(years=1)]
    st.subheader("3. Unemployment rate")
    k1, k2 = st.columns(2)
    k1.metric(f"{d:%b %Y}", f"{v:.1f}%", f"{v - prev:+.1f} pp vs prior month",
              delta_color="inverse")
    if not yr.empty:
        k2.metric("A year earlier", f"{yr['value'].iloc[0]:.1f}%")
    fig = go.Figure(_line(go, df[df.date >= view_start], "Unemployment rate", C1, ".1f"))
    _base_layout(fig, "Percent of labour force")
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, width="stretch")
    _meta_block(st, [
        ("Units", s.units), ("Frequency / adjustment", f"{s.frequency}; {s.adjustment}"),
        ("Source", f"[{s.source_label}]({s.url})"),
        ("Latest observation", _period_label(d, s.frequency)),
        ("Transformation", "None (published level)."),
    ])


def chart_rates(st, go, obs, view_start, fx_invert):
    pol, y2, y10 = (series_frame(obs, k) for k in ("policy", "gov2y", "gov10y"))
    if pol.empty and y2.empty:
        return st.info("Interest-rate data unavailable.")
    st.subheader("4. Monetary conditions: policy rate and bond yields")
    k = st.columns(4)
    for col, (df, lab) in zip(k, [(pol, "Policy rate"), (y2, "2-year yield"),
                                  (y10, "10-year yield")]):
        if not df.empty:
            dd, vv = _latest(df)
            col.metric(f"{lab}, {dd:%d %b %Y}", f"{vv:.2f}%")
    if not y2.empty and not y10.empty:
        sp = y10.merge(y2, on="date", suffixes=("_10", "_2"))
        k[3].metric("10y − 2y spread",
                    f"{(sp['value_10'].iloc[-1] - sp['value_2'].iloc[-1]) * 100:+.0f} bp")
    fig = go.Figure()
    fig.add_trace(_line(go, pol[pol.date >= view_start], "Policy rate (target overnight)",
                        C1, ".2f", step=True))
    fig.add_trace(_line(go, y2[y2.date >= view_start], "2-year GoC yield", C2, ".2f", width=1.5))
    fig.add_trace(_line(go, y10[y10.date >= view_start], "10-year GoC yield", C3, ".2f", width=1.5))
    _base_layout(fig, "Percent")
    st.plotly_chart(fig, width="stretch")
    d = max(df["date"].iloc[-1] for df in (pol, y2, y10) if not df.empty)
    _meta_block(st, [
        ("Units", "Percent per annum"),
        ("Frequency", "Daily (business days); policy rate drawn as a step line"),
        ("Source", "Bank of Canada Valet: [V39079]"
         f"({SERIES['policy'].url}), [BD.CDN.2YR.DQ.YLD and BD.CDN.10YR.DQ.YLD]"
         f"({SERIES['gov2y'].url})"),
        ("Latest observation", f"{d:%Y-%m-%d}"),
        ("Transformation", "None. Spread = 10-year minus 2-year yield, in basis "
         "points. Benchmark yields are mid-market closes of selected GoC issues; "
         "the issue rolls periodically."),
    ])


def chart_fx(st, go, obs, view_start, fx_invert):
    s = SERIES["usdcad"]
    df = series_frame(obs, "usdcad")
    if df.empty:
        return st.info("Exchange-rate data unavailable.")
    if fx_invert:
        df = df.assign(value=1 / df["value"])
        lab, units, fmt = "CAD/USD", "US dollars per 1 Canadian dollar", ".4f"
        note = "Rising line = Canadian dollar appreciating."
    else:
        lab, units, fmt = "USD/CAD", s.units, ".4f"
        note = "Rising line = Canadian dollar depreciating (more CAD per USD)."
    d, v = _latest(df)
    yr = df[df.date <= d - pd.DateOffset(years=1)]
    st.subheader("5. Exchange rate")
    k1, k2 = st.columns(2)
    k1.metric(f"{lab}, {d:%d %b %Y}", f"{v:.4f}")
    if not yr.empty:
        k2.metric("Change over 12 months", f"{100 * (v / yr['value'].iloc[-1] - 1):+.1f}%")
    fig = go.Figure(_line(go, df[df.date >= view_start], lab, C1, fmt, width=1.5))
    _base_layout(fig, units)
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, width="stretch")
    _meta_block(st, [
        ("Quotation convention", "Bank of Canada quotes 1 unit of foreign currency "
         "in Canadian dollars, i.e. USD/CAD = CAD per 1 USD. " + note),
        ("Units", units), ("Frequency", "Daily average rate, business days "
                           "(published by 16:30 ET)"),
        ("Source", f"[{s.source_label}]({s.url})"),
        ("Latest observation", f"{d:%Y-%m-%d}"),
        ("Transformation", "None" if not fx_invert else
         "Inverted: CAD/USD = 1 ÷ USD/CAD (the Bank also publishes this)."),
    ])


INDICATORS = {
    "Real GDP growth": chart_gdp,
    "Inflation (headline & core CPI)": chart_cpi,
    "Unemployment rate": chart_unemp,
    "Policy rate & bond yields": chart_rates,
    "Exchange rate (USD/CAD)": chart_fx,
}


def _render_data_tab(st, obs, meta):
    st.subheader("Series catalogue")
    rows = []
    for key, s in SERIES.items():
        m = meta.get("series", {}).get(key, {})
        rows.append({
            "Series": s.name, "Code": s.code, "Units": s.units,
            "Frequency": s.frequency, "Adjustment": s.adjustment,
            "Source": s.source_label, "First obs.": m.get("first_obs"),
            "Latest obs.": m.get("latest_obs"),
            "Latest value": m.get("latest_value"),
            "StatCan release time (ET)": m.get("release_time"),
            "Fetch status": m.get("status"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.subheader("Verification against original publications")
    st.caption("Each refresh compares one or more observations per series with "
               "values read directly from the publisher's own release pages. "
               "'DIFFERS (likely revised)' flags series the publisher routinely "
               "revises; update REFERENCE_CHECKS in app.py after confirming.")
    ver = meta.get("verification", [])
    if ver:
        st.dataframe(pd.DataFrame(ver)[["series", "date", "kind", "value",
                                        "observed", "status", "source", "url"]],
                     hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("Link"),
                                    "value": "Reference", "observed": "Retrieved"})

    st.subheader("Recent refresh log")
    if LOG_FILE.exists():
        tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-40:]
        st.code("\n".join(tail), language="text")

    st.subheader("Download")
    st.download_button("Download all observations (CSV)",
                       obs.to_csv(index=False).encode(), "canada_observations.csv",
                       "text/csv")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def _running_in_streamlit() -> bool:
    try:
        from streamlit.runtime import exists
        return exists()
    except Exception:                                          # noqa: BLE001
        return False


def cli() -> int:
    p = argparse.ArgumentParser(description="Canada Economic Monitor")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--refresh", action="store_true", help="refresh once and exit")
    g.add_argument("--verify", action="store_true", help="verify cached data")
    g.add_argument("--loop", action="store_true", help="run the scheduler forever")
    a = p.parse_args()
    setup_logging(to_stdout=True)
    if a.refresh:
        meta = refresh("cli")
        return 0 if meta.get("last_status") == "success" else 1
    if a.verify:
        for v in run_verification(load_observations()):
            print(f"{v['series']:<10} {v['date']} {v['kind']:<5} ref={v['value']:<10g} "
                  f"got={v['observed']!s:<10} {v['status']}")
        return 0
    log.info("Headless scheduler: refresh every %g h", REFRESH_HOURS)
    scheduler_loop("cli-loop")
    return 0


if _running_in_streamlit():
    run_dashboard()
elif __name__ == "__main__":
    sys.exit(cli())

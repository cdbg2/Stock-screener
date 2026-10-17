"""Conservative annual financials from the SEC's public EDGAR JSON APIs.

Only USD, entity-wide US-GAAP facts are supported. Missing fields stay missing.
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import time

import requests

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
DEFAULT_USER_AGENT = "ChasDeistStockScreener/2.0 (https://github.com/cdbg2/Stock-screener)"
INCOME_TAGS = ("NetIncomeLoss",)
INTEREST_TAGS = ("InterestExpense", "InterestAndDebtExpense", "InterestExpenseNonoperating")


class SecAccessError(RuntimeError):
    """SEC access or rate limiting failed: stop rather than publish an outage."""


@dataclass(frozen=True)
class Fact:
    value: float
    start: str
    end: str
    filed: str
    accession: str
    tag: str


def points(facts, tags, unit="USD", annual=True):
    """Prefer the first supported tag for each period, then its latest restatement.

    Use actual dates, not `fy`: comparative facts can carry the filing's fiscal
    year instead of the fiscal year to which the fact relates.
    """
    result = {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        per_tag = {}
        for item in gaap.get(tag, {}).get("units", {}).get(unit, []):
            if item.get("form") not in ANNUAL_FORMS:
                continue
            try:
                end = date.fromisoformat(item["end"])
                start = item.get("start", "")
                if annual:
                    days = (end - date.fromisoformat(start)).days
                    if not 330 <= days <= 380:
                        continue
                elif start:
                    continue
                value = float(item["val"])
                if not math.isfinite(value):
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            fact = Fact(value, start, end.isoformat(), item.get("filed", ""),
                        item.get("accn", ""), tag)
            key = (fact.start, fact.end)
            if key not in per_tag or (fact.filed, fact.accession) > (per_tag[key].filed, per_tag[key].accession):
                per_tag[key] = fact
        for key, fact in per_tag.items():
            result.setdefault(key, fact)
    return result


def latest_annual(submissions):
    recent = submissions.get("filings", {}).get("recent", {})
    rows = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in ANNUAL_FORMS:
            continue
        def field(name):
            values = recent.get(name, [])
            return values[i] if i < len(values) else ""
        if field("reportDate"):
            rows.append({"end": field("reportDate"), "filed": field("filingDate"),
                         "accession": field("accessionNumber"), "form": form})
    return max(rows, key=lambda r: (r["end"], r["filed"])) if rows else None


def extract_financials(facts, submissions, today=None):
    """Return matched annual metrics with provenance; never substitute net interest."""
    today = today or date.today()
    report = latest_annual(submissions)
    result = {"company": facts.get("entityName", submissions.get("name", "")),
              "period_end": report["end"] if report else "", "net_income": None,
              "equity": None, "operating_income": None, "interest_expense": None,
              "eps": [], "tags": {}, "filing": report or {}, "issues": []}
    if not report:
        result["issues"].append("No recent annual filing in SEC submissions")
        return result
    if not facts.get("facts", {}).get("us-gaap"):
        result["issues"].append("US-GAAP facts unavailable (IFRS/custom tags not mapped)")
        return result
    end = report["end"]
    if not 0 <= (today - date.fromisoformat(end)).days <= 550:
        result["issues"].append("Annual reporting period is stale or in the future")
        return result
    income = [f for f in points(facts, INCOME_TAGS).values() if f.end == end]
    if not income:
        result["issues"].append("Net income for latest annual filing unavailable in USD")
        return result
    anchor = max(income, key=lambda f: (f.filed, f.start))
    result["net_income"] = anchor.value
    result["tags"]["net_income"] = anchor.__dict__
    for name, tags, annual in [
        ("operating_income", ("OperatingIncomeLoss",), True),
        ("interest_expense", INTEREST_TAGS, True),
        ("equity", ("StockholdersEquity",), False),
    ]:
        selected = points(facts, tags, annual=annual).get((anchor.start if annual else "", end))
        if selected:
            result[name] = selected.value
            result["tags"][name] = selected.__dict__
        else:
            result["issues"].append(f"{name.replace('_', ' ').capitalize()} unavailable for matching annual period")

    # One consistent EPS definition, six annual observations for five YoY checks.
    # Do not patch holes in diluted EPS history using basic EPS from other years.
    for tag in ("EarningsPerShareDiluted", "EarningsPerShareBasic"):
        values = points(facts, (tag,), unit="USD/shares")
        current = values.get((anchor.start, end))
        if current is None:
            continue
        by_end = {}
        for fact in values.values():
            if fact.end <= end and (fact.end not in by_end or fact.filed > by_end[fact.end].filed):
                by_end[fact.end] = fact
        series = sorted(by_end.values(), key=lambda f: f.end)[-6:]
        contiguous = len(series) == 6 and all(
            330 <= (date.fromisoformat(b.start) - date.fromisoformat(a.start)).days <= 400
            and abs((date.fromisoformat(b.start) - date.fromisoformat(a.end)).days) <= 7
            for a, b in zip(series, series[1:]))
        if contiguous:
            # Older comparative EPS can predate a split while newer EPS is
            # restated. Never compare those incompatible share bases or guess
            # an adjustment from potentially duplicated event disclosures.
            splits = facts.get("facts", {}).get("us-gaap", {}).get(
                "StockholdersEquityNoteStockSplitConversionRatio1", {}).get("units", {}).get("pure", [])
            split_dates = [s.get("end", "") for s in splits
                           if s.get("val", 1) != 1 and series[0].end <= s.get("end", "") <= end]
            last_split = max(split_dates, default="")
            if last_split and any(f.filed < last_split for f in series):
                result["issues"].append("EPS history spans a reported stock split without a comparable restated six-year series")
            else:
                result["eps"] = [f.value for f in series]
                result["tags"]["eps"] = [f.__dict__ for f in series]
        else:
            result["issues"].append("Six consecutive annual EPS values unavailable for five YoY growth checks")
        break
    else:
        result["issues"].append("EPS for latest annual period unavailable")
    return result


class SecClient:
    """Sequential requests at <=4/sec, with bounded retries and a daily disk cache."""
    def __init__(self, user_agent=None, cache_dir=".cache/sec"):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent or os.environ.get("SEC_USER_AGENT") or DEFAULT_USER_AGENT,
                                     "Accept-Encoding": "gzip, deflate"})
        self.cache_dir = Path(cache_dir)
        self.last_request = 0.0
        self.tickers = None
        self.companies = {}

    def get_json(self, url, key):
        path = self.cache_dir / f"{key}.json"
        day = datetime.now(timezone.utc).date().isoformat()
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                if cached.get("day") == day:
                    return cached["data"]
            except (ValueError, KeyError):
                pass
        for attempt in range(3):
            time.sleep(max(0, .25 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=(10, 60))
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise SecAccessError("SEC connection failed after three attempts") from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 403:
                raise SecAccessError("SEC access denied (HTTP 403); check SEC_USER_AGENT and runner access")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise SecAccessError(f"SEC unavailable after three attempts (HTTP {response.status_code})")
                try:
                    delay = min(30, max(1, float(response.headers.get("Retry-After", 2 ** (attempt + 1)))))
                except ValueError:
                    delay = 2 ** (attempt + 1)
                time.sleep(delay)
                continue
            response.raise_for_status()
            data = response.json()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"day": day, "data": data}))
            return data
        raise SecAccessError("SEC request failed")

    def load_tickers(self):
        if self.tickers is None:
            data = self.get_json("https://www.sec.gov/files/company_tickers.json", "tickers")
            self.tickers = {r["ticker"].upper().replace(".", "-"): r for r in data.values()}
        return self.tickers

    def financials(self, symbol):
        row = self.load_tickers().get(symbol.upper().replace(".", "-"))
        if row is None:
            raise ValueError("Ticker not found in SEC ticker map")
        cik = str(row["cik_str"]).zfill(10)
        if cik not in self.companies:
            facts = self.get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", f"facts-{cik}")
            submissions = self.get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", f"submissions-{cik}")
            metrics = extract_financials(facts, submissions)
            metrics["cik"] = cik
            metrics["source_url"] = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
            self.companies[cik] = metrics
        return self.companies[cik]

"""Read date-filtered earnings releases and their explicitly stated outlook."""

import calendar
import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import sec_edgar

_SUBMISSIONS = "https://data.sec.gov/submissions/"
_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/"
_DATE = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}"
)
_QUARTERS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_QUARTER = r"(?:(first|second|third|fourth)[ -]quarter|Q([1-4])\b)"
_YEAR = r"(?:fiscal(?: year)?\s+|FY\s*)(20\d{2})"
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_AMOUNT = rf"(?:(?:US\$|USD|\$)\s*)?({_NUMBER})(?:\s+(billion|million))?"
_BLOCK_TAGS = ("p", "div", "li", "h1", "h2", "h3", "h4")


def _parse_date(text):
    try:
        return datetime.strptime(text.replace(",", ""), "%B %d %Y").date()
    except ValueError:
        return None


def _filings(payload, as_of_date):
    keys = ("form", "filingDate", "accessionNumber", "items")
    for form, filed, accession, items in zip(*(payload.get(key, []) for key in keys), strict=False):
        if (filed <= as_of_date and form in {"8-K", "8-K/A"}
                and re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)):
            relevant = set(re.findall(r"\d\.\d{2}", items))
            if relevant & {"2.02", "7.01"}:
                yield filed, accession, relevant


def _releases(cik, as_of_date):
    data = sec_edgar._cached_json(f"{_SUBMISSIONS}CIK{cik}.json", f"submissions_{cik}.json")
    earliest = (date.fromisoformat(as_of_date) - timedelta(days=180)).isoformat()
    filings = list(_filings(data.get("filings", {}).get("recent", {}), as_of_date))
    for archive in data.get("filings", {}).get("files", []):
        name = archive.get("name", "")
        if (archive.get("filingFrom", "") <= as_of_date and archive.get("filingTo", "") >= earliest
                and re.fullmatch(rf"CIK{cik}-submissions-\d+\.json", name)):
            filings.extend(_filings(sec_edgar._cached_json(_SUBMISSIONS + name, name), as_of_date))
    return sorted((row for row in filings if row[0] >= earliest), key=lambda row: row[:2], reverse=True)


def _exhibits(cik, accession):
    base = f"{_ARCHIVES}{int(cik)}/{accession.replace('-', '')}/"
    index = BeautifulSoup(sec_edgar._request(f"{base}{accession}-index.htm").text, "html.parser")
    for row in index.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4 or not re.fullmatch(r"EX-99(?:\.\d+)?", cells[3].get_text(strip=True)):
            continue
        link = cells[2].find("a", href=True)
        if not link:
            continue
        url = urljoin(base, link["href"])
        if url.startswith(base) and re.fullmatch(r"[\w.-]+\.html?", urlsplit(url).path.rsplit("/", 1)[-1]):
            yield url


def _blocks(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "ix:header"]):
        tag.decompose()
    return [" ".join(tag.get_text(" ", strip=True).split()) for tag in soup.find_all(_BLOCK_TAGS)
            if not tag.find(_BLOCK_TAGS)]


def _period(text):
    quarter = re.search(_QUARTER, text, re.I)
    year = re.search(_YEAR, text, re.I)
    if not year:
        return None
    number = None
    if quarter:
        number = _QUARTERS[quarter[1].lower()] if quarter[1] else int(quarter[2])
    return int(year[1]), number


def _period_ends(context, period, document):
    ending = re.search(rf"\bend(?:s|ing)?\s+(?:on\s+)?({_DATE})", context, re.I)
    if ending and (day := _parse_date(ending[1])):
        return [day.isoformat()]

    # Some releases identify the next quarter only by number. Anchor it to
    # the immediately preceding reported quarter in the same release.
    reported = re.search(rf"{_QUARTER}\s+(?:period\s+)?ended\s+({_DATE})", document, re.I)
    if not reported or not period[1]:
        return []
    previous = _QUARTERS[reported[1].lower()] if reported[1] else int(reported[2])
    reported_end = _parse_date(reported[3])
    prior_context = _period(document.partition(reported[0])[0][:650])
    expected_prior = (period[0] - 1, 4) if period[1] == 1 else (period[0], period[1] - 1)
    if not reported_end or previous != expected_prior[1] or prior_context != expected_prior:
        return []
    if reported_end.day == calendar.monthrange(reported_end.year, reported_end.month)[1]:
        year, month = divmod(reported_end.year * 12 + reported_end.month + 2, 12)
        return [date(year, month + 1, calendar.monthrange(year, month + 1)[1]).isoformat()]
    return [(reported_end + timedelta(weeks=weeks)).isoformat() for weeks in (13, 14)]


def _bounds(text, metric):
    # Require a forward-looking statement and anchor the number to the named metric.
    name = r"(?:net |total )?(?:revenue|sales)" if metric == "revenue" else (
        r"(?:(?:diluted )?earnings per (?:diluted )?share|(?:diluted )?EPS)"
    )
    match = re.search(
        rf"\b{name}\b(?:\s*\((?:GAAP|non-GAAP|adjusted)\))?\s+(?:(?:is|are)\s+)?"
        rf"(?:(?:expected|projected|forecast)\s+(?:to be|to range from|at|in)|(?:of|between|from))\s+"
        rf"(?:in (?:the )?range of\s+|between\s+|approximately\s+|about\s+)?{_AMOUNT}"
        rf"(?:\s*(?:to|and|–|—|-)\s*{_AMOUNT})?"
        rf"(?:\s*,?\s*(?:plus or minus|\+/-|±)\s*({_NUMBER})\s*%)?",
        text, re.I,
    )
    if not match:
        return None
    first, first_unit, second, second_unit, percent = match.groups()
    scale = {None: 1, "million": 1_000_000, "billion": 1_000_000_000}
    unit = (first_unit or second_unit or "").lower() or None
    if (metric == "revenue") != bool(unit):
        return None
    low = float(first.replace(",", "")) * scale[(first_unit or unit or "").lower() or None]
    high = (float(second.replace(",", "")) * scale[(second_unit or unit or "").lower() or None]
            if second else low)
    if percent:
        margin = low * float(percent.replace(",", "")) / 100
        low, high = low - margin, high + margin
    if low > high:
        return None
    return {"low": round(low, 6), "high": round(high, 6)}


def _guidance(blocks, url, filed):
    document = " ".join(blocks)
    for index, block in enumerate(blocks):
        if not re.match(r"^(?:(?:FY\s*20\d{2}|20\d{2}|\w+ quarter)\s+)?"
                        r"(?:financial )?(?:outlook|guidance)\b", block, re.I):
            continue
        if len(block) > 220:
            continue
        records = []
        context = block
        period = _period(context)
        for line in blocks[index:index + 12]:
            if line != block and re.match(r"^(?:about |forward-looking|non-gaap measures|for further)", line, re.I):
                break
            if new_period := _period(line):
                period, context = new_period, line
            if not period:
                continue
            for metric in ("revenue", "eps"):
                bounds = _bounds(line, metric)
                if not bounds:
                    continue
                basis = None
                if metric == "eps":
                    if re.search(r"non[ -]GAAP|adjusted", line, re.I):
                        basis = "normalized"
                    elif re.search(r"\bGAAP\b", line, re.I):
                        basis = "gaap"
                # Dollar signs alone are ambiguous for issuers reporting in
                # CAD/AUD and other dollars. Resolve the unit in the caller.
                currency = "USD" if re.search(r"\bUSD\b|US\$|U\.S\. dollars", line, re.I) else None
                year, quarter = period
                records.append({
                    "metric": metric, "basis": basis, "currency": currency,
                    "uses_dollar_sign": "$" in line,
                    "fiscal_period": f"FY{year}-Q{quarter}" if quarter else f"FY{year}",
                    "period_ends": _period_ends(context, period, document),
                    "period_type": "quarter" if quarter else "year",
                    "filed_on": filed, "source_url": url, **bounds,
                })
        if records:
            return records
    return []


def _confirmed_date(blocks, as_of_date):
    for text in blocks[:40]:
        match = re.search(
            rf"\b(?:will|to)\s+(?:announce|report|release)\b.{{0,125}}"
            rf"\b(?:results|earnings)\b.{{0,100}}?\bon\s+(?:(?:\w+day),?\s+)?({_DATE})",
            text, re.I,
        )
        if match and (day := _parse_date(match[1])) and day.isoformat() >= as_of_date:
            return day.isoformat()
    return None


def _reporting_currency(cik, filed):
    data = sec_edgar._cached_json(sec_edgar._FACTS_URL.format(cik=cik), f"CIK{cik}.json")
    facts = data.get("facts", {}).get("us-gaap", {})
    currencies = set()
    for tag in sec_edgar._STATEMENTS["income_statement"][0][1]:
        for unit, values in facts.get(tag, {}).get("units", {}).items():
            if any(value.get("filed") == filed for value in values):
                currencies.add(unit)
    return currencies.pop() if len(currencies) == 1 else None


def get_company_guidance(ticker, as_of_date=None):
    day = as_of_date or get_current_date()
    cik = sec_edgar.cik_for(ticker)
    if not cik:
        return {"source": "SEC EDGAR", "unavailable": "No SEC filer found for this symbol."}
    result = {"source": "SEC EDGAR", "guidance": []}
    releases = _releases(cik, day)
    last_results = next((row for row in releases if "2.02" in row[2]), None)
    updates = [row for row in releases if last_results is None or row[:2] > last_results[:2]]
    selected = updates[:7] + ([last_results] if last_results else [])
    withdrawn = False
    for filed, accession, _ in selected:
        for url in _exhibits(cik, accession):
            blocks = _blocks(sec_edgar._request(url).text)
            if "confirmed_earnings" not in result and (confirmed := _confirmed_date(blocks, day)):
                result["confirmed_earnings"] = {
                    "dates": [confirmed], "status": "confirmed", "published_on": filed, "source_url": url,
                }
            if not result["guidance"] and not withdrawn:
                result["guidance"] = _guidance(blocks, url, filed)
                if not result["guidance"] and any(re.search(
                    r"\bwithdraw(?:s|ing|n)?\s+(?:(?:its|our|previously issued|previous)\s+){0,3}"
                    r"(?:financial\s+)?(?:outlook|guidance)\b", block, re.I,
                ) for block in blocks):
                    withdrawn = True
                    result["guidance_unavailable"] = f"Company withdrew guidance in its {filed} release."
                    result["withdrawal_source_url"] = url
    records = result["guidance"]
    if any(row["uses_dollar_sign"] and row["currency"] is None for row in records):
        try:
            currency = _reporting_currency(cik, records[0]["filed_on"])
        except VendorUnavailableError:
            currency = None
        for row in records:
            if row["uses_dollar_sign"] and currency in {"USD", "CAD", "AUD", "NZD", "HKD", "SGD"}:
                row["currency"] = row["currency"] or currency
    for row in records:
        del row["uses_dollar_sign"]
    if not records and not withdrawn:
        result["guidance_unavailable"] = "No supported numeric revenue or EPS outlook found in the latest releases."
    return result

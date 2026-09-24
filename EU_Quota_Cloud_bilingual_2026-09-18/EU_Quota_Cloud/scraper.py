"""
EU Steel Safeguard TRQ Scraper
=================================

Fetches every order number's "Tariff quota details" page from
https://ec.europa.eu/taxation_customs/dds2/taric/quota_tariff_details.jsp,
parses it, and computes the same fields your spreadsheet used to track
by hand.

Field mapping (confirmed against a real sample page + your spreadsheet):
  Initial amount              -> 분기 쿼터
  Total awaiting allocation   -> 대기할당
  Balance                     -> 잔여 쿼터
  Balance - awaiting          -> 실제잔여 쿼터
  Amount (initial + carry-over) -> 가용 쿼터
  Transferred Amount          -> 이월량
  actual_remaining / amount   -> 잔여율
  1 - remaining_rate          -> 소진율
  Exhaustion date             -> 소진 날짜

Individual order-number failures do NOT stop the run — each one is
retried a couple of times, then logged as an error and skipped so the
rest can continue. See scrape_all() for the success/failure bookkeeping,
and run_daily.py for the "don't overwrite good data with a bad run" logic.
"""

try:
    import requests
    from bs4 import BeautifulSoup
except ModuleNotFoundError:  # Allows parser-only unit tests before dependencies are installed.
    requests = None
    BeautifulSoup = None
import re
import json
import os
import time
from datetime import datetime, date

HERE = os.path.dirname(__file__)
REFERENCE_FILE = os.path.join(HERE, "data", "reference_categories.json")
OUTPUT_JSON = os.path.join(HERE, "data", "quotas.json")

BASE_URL = "https://ec.europa.eu/taxation_customs/dds2/taric/quota_tariff_details.jsp"
CONSULTATION_URL = "https://ec.europa.eu/taxation_customs/dds2/taric/quota_consultation.jsp?Lang=en"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalSafeguardQuotaMonitor/1.0)"
}

# Delay between requests so we don't hammer the site (~100 requests/run)
REQUEST_DELAY_SECONDS = 1.5

# (connect_timeout, read_timeout) — connect fails fast, read allows the
# server a bit more time to respond.
# (connect_timeout, read_timeout) — connect fails fast, read allows the
# server a bit more time to respond. Bumped from 15s after real runs showed
# several read timeouts on a normal (non-outage) day.
REQUEST_TIMEOUT = (5, 20)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2  # doubles each retry (2s, 4s, 8s) — see fetch_detail_page

LABELS_IN_ORDER = [
    "Order number",
    "Validity period",
    "Origin",
    "Initial amount",
    "Amount",
    "Balance",
    "Transferred Amount",
    "Exhaustion date",
    "Critical",
    "Last import date",
    "Last allocation date",
    "Total awaiting allocation (indicative)",
    "Blocking period",
    "Suspension period",
    "Allocated percentage at the last allocation",
    "Associated TARIC code",
]


def current_quarter_start(as_of: date = None) -> str:
    """
    EU safeguard TRQ quarters run 1 Jan / 1 Apr / 1 Jul / 1 Oct.
    Returns the start date of the current quarter as YYYY-MM-DD,
    which is what the StartDate= URL param expects.
    """
    as_of = as_of or date.today()
    q_month = ((as_of.month - 1) // 3) * 3 + 1
    return date(as_of.year, q_month, 1).isoformat()


def current_quarter_label(as_of: date = None) -> str:
    """'2026 Q3'"""
    as_of = as_of or date.today()
    q = (as_of.month - 1) // 3 + 1
    return f"{as_of.year} Q{q}"


def build_url(order_number: str, start_date: str) -> str:
    return f"{BASE_URL}?Lang=en&StartDate={start_date}&Code={order_number}"


def require_runtime_dependencies():
    if requests is None or BeautifulSoup is None:
        raise RuntimeError("Install runtime packages with: pip install -r requirements.txt")


def fetch_detail_page(order_number: str, start_date: str) -> str:
    """
    Fetch one order number's page, retrying on timeout/connection errors
    with exponential backoff (2s, 4s, 8s, ...). Also catches raw OSError
    (e.g. Windows' ConnectionAbortedError / WinError 10053) in case it
    isn't wrapped by requests as a ConnectionError — seen in real runs.
    """
    require_runtime_dependencies()
    url = build_url(order_number, start_date)
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 2):  # e.g. MAX_RETRIES=3 -> attempts 1,2,3,4
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, OSError) as e:
            last_exc = e
            if attempt <= MAX_RETRIES:
                delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))  # 2s, 4s, 8s
                print(f"      ... network error, retrying ({attempt}/{MAX_RETRIES}) in {delay}s")
                time.sleep(delay)
                continue
            raise
        except requests.exceptions.HTTPError as e:
            # HTTP errors (500, 404, etc.) aren't worth retrying
            raise

    raise last_exc


def parse_number(text: str):
    """
    Parse a number from text like '18829770  Kilogram', '10,000', '10 000',
    '117 559.123'. Returns None if no number is found (caller must not
    treat that the same as a real 0 — see parse_quota_detail).

    Convention: '.' is always treated as a decimal point (never a thousands
    separator), consistent with every real value observed from this site
    (e.g. "86.798" for a percentage). ',' and plain/non-breaking spaces are
    treated as thousands separators and stripped.
    """
    if not text:
        return None
    cleaned = text.strip().replace(",", "").replace("\xa0", "").replace(" ", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    return float(match.group(0))


def parse_ddmmyyyy(text: str):
    """'10-08-2026' -> '2026-08-10' ; '' -> None"""
    if not text:
        return None
    match = re.search(r"(\d{2})-(\d{2})-(\d{4})", text)
    if not match:
        return None
    d, m, y = match.groups()
    return f"{y}-{m}-{d}"


def parse_tariff_quota_update(text: str):
    """Extract the EU site's global ``Last tariff quota update`` date.

    The label is printed on every tariff-quota detail page and is distinct
    from the time at which this dashboard happens to run.
    """
    match = re.search(
        r"Last\s+tariff\s+quota\s+update\s*[:：]?\s*(\d{2}[-/.]\d{2}[-/.]\d{4})",
        text or "",
        re.IGNORECASE,
    )
    return parse_ddmmyyyy(match.group(1).replace("/", "-").replace(".", "-")) if match else None


def fetch_tariff_quota_update() -> str:
    """Fetch the official global update date from the consultation page.

    The date is displayed on the main quota-consultation page, not reliably
    on every order-number detail page. Fetching it once also avoids making
    the 101 detail requests responsible for this global metadata field.
    """
    require_runtime_dependencies()
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = requests.get(
                CONSULTATION_URL,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            text = BeautifulSoup(response.text, "html.parser").get_text(
                separator=" ", strip=True
            )
            update_date = parse_tariff_quota_update(text)
            if not update_date:
                raise ValueError(
                    "official 'Last tariff quota update' date was not found"
                )
            return update_date
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            OSError,
        ) as exc:
            last_exc = exc
            if attempt <= MAX_RETRIES:
                delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                print(
                    "      ... EU update-date network error, "
                    f"retrying ({attempt}/{MAX_RETRIES}) in {delay}s"
                )
                time.sleep(delay)
                continue
            raise

    raise last_exc


def parse_validity_period(text: str):
    """'01-07-2026  -  30-09-2026' -> ('2026-07-01', '2026-09-30')"""
    matches = re.findall(r"\d{2}-\d{2}-\d{4}", text or "")
    if len(matches) >= 2:
        return parse_ddmmyyyy(matches[0]), parse_ddmmyyyy(matches[1])
    return None, None


def extract_fields(page_text: str) -> dict:
    """
    Extract label -> value by finding the text between each known label
    and the next known label. Works on the page's visible text, so it's
    resilient to the exact HTML tag structure.

    "Total awaiting allocation" is matched with a flexible regex (the
    "(indicative)" suffix is treated as optional) because a mismatch here
    was found to silently produce wrong data — see parse_quota_detail's
    awaiting-allocation handling below.
    """
    values = {}
    for i, label in enumerate(LABELS_IN_ORDER):
        next_label = LABELS_IN_ORDER[i + 1] if i + 1 < len(LABELS_IN_ORDER) else None

        if label == "Total awaiting allocation (indicative)":
            label_pattern = r"Total\s+awaiting\s+allocation(?:\s*\(indicative\))?\s*:?"
        elif label == "Amount":
            # Plain "Amount" must not match the tail of "Initial amount".
            label_pattern = r"(?<!Initial\s)Amount"
        else:
            label_pattern = re.escape(label)

        if next_label:
            pattern = label_pattern + r"(.*?)" + re.escape(next_label)
        else:
            pattern = label_pattern + r"(.*)"
        m = re.search(pattern, page_text, re.DOTALL | re.IGNORECASE)
        values[label] = m.group(1).strip() if m else None  # None = label genuinely not found
    return values


def parse_quota_detail(html_or_text: str, order_number: str) -> dict:
    """
    Accepts either raw HTML (parsed with BeautifulSoup to get visible text)
    or already-extracted plain text.

    Raises ValueError if a required field (Order number, or Total awaiting
    allocation) cannot be found at all — this is treated as a failed record
    upstream (scrape_all), NOT silently defaulted, per the explicit
    requirement that "field not found" and "field found and equals 0" must
    never be conflated.
    """
    if "<html" in html_or_text.lower() or "<table" in html_or_text.lower():
        require_runtime_dependencies()
        soup = BeautifulSoup(html_or_text, "html.parser")
        text = soup.get_text(separator="\n")
    else:
        text = html_or_text

    fields = extract_fields(text)

    def field(label):
        """Safe accessor: returns '' for 'found but blank', None for 'label not found at all'."""
        return fields.get(label)

    order_number_field = field("Order number")
    if not order_number_field or not order_number_field.strip():
        raise ValueError("parsing error: page did not contain expected fields")

    start_date, end_date = parse_validity_period(field("Validity period") or "")
    initial_amount = parse_number(field("Initial amount") or "")
    # Amount = Initial amount + Transferred Amount (carry-over from the previous
    # quarter). A blank Transferred Amount means no carry-over.
    amount = parse_number(field("Amount") or "") or initial_amount
    transferred = parse_number(field("Transferred Amount") or "") or 0.0
    balance = parse_number(field("Balance") or "")
    exhaustion_date = parse_ddmmyyyy(field("Exhaustion date") or "")

    # --- Total awaiting allocation: the field genuinely being absent from
    # the page is a DIFFERENT situation from the page saying its value is 0.
    # Conflating them (e.g. "value or 0.0") was the root cause of a bug
    # where every record silently showed 대기할당=0 regardless of the
    # real value. We now raise instead, so the record surfaces as a failed
    # scrape (see scrape_all) rather than polluting the dashboard with a
    # fabricated zero.
    awaiting_raw = field("Total awaiting allocation (indicative)")
    if awaiting_raw is None:
        raise ValueError(
            f"parsing error: 'Total awaiting allocation' field not found for order {order_number}"
        )
    awaiting = parse_number(awaiting_raw)
    if awaiting is None:
        # Label was found but its value couldn't be parsed as a number
        # (e.g. unexpected text) — still an error, not a silent 0.
        raise ValueError(
            f"parsing error: 'Total awaiting allocation' value unparseable "
            f"({awaiting_raw!r}) for order {order_number}"
        )

    actual_remaining = None
    remaining_rate = None
    consumption_rate = None
    if balance is not None:
        actual_remaining = balance - awaiting
        if amount:
            remaining_rate = actual_remaining / amount
            consumption_rate = 1 - remaining_rate

    # "Exhausted" for row-styling purposes means fully consumed (100%),
    # not merely "has an exhaustion date" — a quota can have an exhaustion
    # date recorded while still having a sliver of balance left, and that
    # should NOT trigger the full-row red treatment.
    is_exhausted = remaining_rate is not None and remaining_rate <= 0

    origin_scraped_raw = (field("Origin") or "").strip()

    return {
        "order_number": order_number,
        # NOTE: this is the RAW text scraped from the site's Origin field.
        # For single-country quotas it's clean (e.g. "ERGA OMNES"), but for
        # multi-country FTA/CSQ quotas the site returns several country
        # names concatenated with no separator. Do NOT use this for display —
        # the display label ("FTA Quota – CSQ", "Türkiye", "Other countries",
        # etc.) is a stable business classification that belongs in
        # reference_categories.json (origin_label), not something to be
        # re-derived from scraping every day. scrape_all() overrides this
        # with the reference's origin_label before saving.
        "origin_scraped_raw": origin_scraped_raw,
        "origin": origin_scraped_raw,  # placeholder; overridden in scrape_all()
        "start_date": start_date,
        "end_date": end_date,
        "quarterly_kg": initial_amount,
        "quarterly_t": (initial_amount / 1000) if initial_amount is not None else None,
        "amount_kg": amount,
        "amount_t": (amount / 1000) if amount is not None else None,
        "transferred_kg": transferred,
        "transferred_t": transferred / 1000,
        "pending_kg": awaiting,
        "pending_t": awaiting / 1000,
        "remaining_kg": balance,
        "remaining_t": (balance / 1000) if balance is not None else None,
        "actual_remaining_kg": actual_remaining,
        "actual_remaining_t": (actual_remaining / 1000) if actual_remaining is not None else None,
        "remaining_rate": remaining_rate,
        "consumption_rate": consumption_rate,
        "exhaustion_date": exhaustion_date,
        "is_exhausted": is_exhausted,
        # Global EU TARIC data-publication date (not the local run time).
        "_tariff_quota_last_updated": parse_tariff_quota_update(text),
    }


def load_reference() -> dict:
    with open(REFERENCE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _format_status_line(index, total, order_number, record=None, error=None):
    prefix = f"[{index}/{total}] {order_number} ..."
    if error:
        return f"{prefix} ERROR - {error}"
    bal_t = record.get("remaining_t")
    bal_str = f"{bal_t:,.0f} t" if bal_t is not None else "-"
    exhausted_str = " (EXHAUSTED)" if record.get("is_exhausted") else ""
    return f"{prefix} OK | Balance: {bal_str}{exhausted_str}"


def scrape_all(start_date: str = None, delay: float = REQUEST_DELAY_SECONDS, limit: int = None, verbose: bool = True) -> dict:
    """
    Fetch and parse every order number in the reference file.
    Individual failures are logged and skipped; the run continues.
    `limit`: for testing, only scrape the first N order numbers.
    """
    start_date = start_date or current_quarter_start()
    reference = load_reference()
    order_numbers = list(reference.keys())
    if limit:
        order_numbers = order_numbers[:limit]

    total = len(order_numbers)
    records = []
    errors = []
    tariff_quota_update_dates = []

    # This is global metadata shown on the consultation page. A failure here
    # must not discard otherwise valid quota records, but it is made visible
    # in the logs instead of silently pretending that the date is absent.
    try:
        tariff_quota_update_dates.append(fetch_tariff_quota_update())
    except Exception as exc:
        if verbose:
            print(f"[EU update date] WARNING - {exc}")

    for i, order_number in enumerate(order_numbers, start=1):
        try:
            html = fetch_detail_page(order_number, start_date)
            parsed = parse_quota_detail(html, order_number)
            site_update = parsed.pop("_tariff_quota_last_updated", None)
            if site_update:
                tariff_quota_update_dates.append(site_update)
            ref = reference.get(order_number, {})
            parsed["category"] = ref.get("category")
            parsed["item_name"] = ref.get("item_name")
            # Origins is a curated business classification (country name,
            # "FTA Quota – CSQ", "Other countries", etc.) — always trust
            # reference_categories.json over whatever the raw scrape found.
            if ref.get("origin_label"):
                parsed["origin"] = ref["origin_label"]
            records.append(parsed)
            if verbose:
                print(_format_status_line(i, total, order_number, record=parsed))
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            errors.append({"order_number": order_number, "error": err_msg})
            if verbose:
                print(_format_status_line(i, total, order_number, error=err_msg))

        if i < total:
            time.sleep(delay)

    payload = {
        "last_updated": datetime.now().isoformat(),
        # All detail pages normally show the same global update date. Taking
        # max() also makes the result robust during a site-side rollover.
        "tariff_quota_last_updated": max(tariff_quota_update_dates) if tariff_quota_update_dates else None,
        "start_date_used": start_date,
        "quarter_label": current_quarter_label(),
        "total_attempted": total,
        "count": len(records),
        "errors": errors,
        "failed_order_numbers": [error["order_number"] for error in errors],
        "records": records,
    }

    return payload


def save_quotas(payload: dict, path: str = None):
    path = path or OUTPUT_JSON
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print("Running scraper for ALL order numbers in reference_categories.json...")
    print(f"Using quarter start date: {current_quarter_start()}")
    result = scrape_all()
    print(f"Done. {result['count']} records fetched, {len(result['errors'])} errors.")
    if result["errors"]:
        for e in result["errors"][:10]:
            print(" -", e)

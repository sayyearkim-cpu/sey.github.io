"""Cloud Run Job entrypoint for the EU steel quota dashboard.

The job scrapes the current EU TARIC quota pages, applies the existing
90-percent safety threshold, and stores one idempotent snapshot per Berlin
calendar day in Cloud Firestore. It exits non-zero when validation fails so
Cloud Run and Cloud Scheduler report the execution as failed.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scraper import scrape_all, current_quarter_label, current_quarter_start


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
SEED_HISTORY_DIR = DATA_DIR / "seed_history"
FTA_ORIGINS_FILE = DATA_DIR / "fta_origins.json"

SUCCESS_THRESHOLD = float(os.getenv("SUCCESS_THRESHOLD", "0.90"))
BERLIN = ZoneInfo("Europe/Berlin")


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_payload(payload: dict, snapshot_date: str | None = None) -> dict:
    """Return the common schema used by Firestore and the dashboard."""
    normalized = dict(payload)
    if "site_last_update" not in normalized:
        normalized["site_last_update"] = normalized.pop(
            "tariff_quota_last_updated", None
        )
    else:
        normalized.pop("tariff_quota_last_updated", None)

    if snapshot_date is None:
        snapshot_date = str(normalized.get("last_updated") or "")[:10]
    normalized["snapshot_date"] = snapshot_date
    normalized["schema_version"] = 1
    normalized["fta_origins"] = load_json(FTA_ORIGINS_FILE)
    normalized["failed_order_numbers"] = [
        error.get("order_number")
        for error in normalized.get("errors", [])
        if error.get("order_number")
    ]
    return normalized


def import_seed_history_once(db) -> int:
    """Import the historical JSON bundled from the desktop version once."""
    marker = db.collection("quota_meta").document("seed_history")
    if marker.get().exists:
        print("Seed history already imported; skipping.")
        return 0

    imported = 0
    for path in sorted(SEED_HISTORY_DIR.glob("*.json")):
        snapshot_date = path.stem
        payload = normalize_payload(load_json(path), snapshot_date)
        db.collection("quota_snapshots").document(snapshot_date).set(payload)
        imported += 1
        print(f"Imported historical snapshot: {snapshot_date}")

    marker.set(
        {
            "completed": True,
            "snapshot_count": imported,
            "completed_at": datetime.now(BERLIN).isoformat(),
        }
    )
    return imported


def build_category_summary(payload: dict) -> dict:
    """Per-category rollup for the dashboard's Compare view.

    A few KB instead of the ~70KB full snapshot, so browsing years of
    history there stays cheap regardless of how much has accumulated —
    see quota_category_summary in firestore.rules.
    """
    categories: dict = {}
    for record in payload.get("records", []):
        category = record.get("category")
        if not category:
            continue
        entry = categories.setdefault(category, {
            "item_name": record.get("item_name"),
            "amount_kg": 0.0,
            "actual_remaining_kg": 0.0,
            "pending_kg": 0.0,
            "transferred_kg": 0.0,
        })
        entry["amount_kg"] += record.get("amount_kg") or record.get("quarterly_kg") or 0.0
        entry["actual_remaining_kg"] += record.get("actual_remaining_kg") or 0.0
        entry["pending_kg"] += record.get("pending_kg") or 0.0
        entry["transferred_kg"] += record.get("transferred_kg") or 0.0
    return {
        "date": payload["snapshot_date"],
        "quarter_label": payload.get("quarter_label"),
        "success_count": payload.get("count", 0),
        "total_attempted": payload.get("total_attempted", 0),
        "failed_order_numbers": payload.get("failed_order_numbers", []),
        "categories": categories,
    }


def backfill_category_summaries_once(db) -> int:
    """One-time v2 refresh adding collection completeness to all summaries."""
    marker = db.collection("quota_meta").document("category_summary_backfill")
    marker_snapshot = marker.get()
    if marker_snapshot.exists and (marker_snapshot.to_dict() or {}).get("version", 0) >= 2:
        return 0

    backfilled = 0
    for doc in db.collection("quota_snapshots").stream():
        payload = doc.to_dict() or {}
        payload.setdefault("snapshot_date", doc.id)
        db.collection("quota_category_summary").document(doc.id).set(build_category_summary(payload))
        backfilled += 1

    marker.set({
        "completed": True,
        "version": 2,
        "backfilled_count": backfilled,
        "completed_at": datetime.now(BERLIN).isoformat(),
    })
    return backfilled


def preserve_last_known_site_update(db, payload: dict) -> str | None:
    """Keep the last verified EU publication date if today's lookup fails."""
    if payload.get("site_last_update"):
        return payload["site_last_update"]

    current = db.collection("quota_meta").document("current").get()
    if current.exists:
        previous = (current.to_dict() or {}).get("site_last_update")
        if previous:
            payload["site_last_update"] = previous
            print(f"EU site update carried forward from Firestore: {previous}")
            return previous

    for path in sorted(SEED_HISTORY_DIR.glob("*.json"), reverse=True):
        previous = load_json(path).get("site_last_update")
        if previous:
            payload["site_last_update"] = previous
            print(f"EU site update carried forward from seed history: {previous}")
            return previous
    return None


def save_snapshot(db, payload: dict) -> str:
    snapshot_date = payload["snapshot_date"]
    snapshot_ref = db.collection("quota_snapshots").document(snapshot_date)
    meta_ref = db.collection("quota_meta").document("current")
    summary_ref = db.collection("quota_category_summary").document(snapshot_date)

    batch = db.batch()
    batch.set(snapshot_ref, payload)
    batch.set(summary_ref, build_category_summary(payload))
    batch.set(
        meta_ref,
        {
            "latest_snapshot": snapshot_date,
            "last_updated": payload.get("last_updated"),
            "site_last_update": payload.get("site_last_update"),
            "quarter_label": payload.get("quarter_label"),
            "total_attempted": payload.get("total_attempted", 0),
            "success_count": payload.get("count", len(payload.get("records", []))),
            "record_count": len(payload.get("records", [])),
            "error_count": len(payload.get("errors", [])),
            "failed_order_numbers": payload.get("failed_order_numbers", []),
        },
    )
    batch.commit()
    return snapshot_date


def validate(payload: dict) -> tuple[bool, float]:
    total = payload.get("total_attempted", 0)
    success = payload.get("count", 0)
    rate = success / total if total else 0.0
    return total > 0 and rate >= SUCCESS_THRESHOLD, rate


def main() -> int:
    from google.cloud import firestore

    now = datetime.now(BERLIN)
    start_date = current_quarter_start(now.date())
    print("=" * 56)
    print("EU QUOTA CLOUD UPDATE")
    print(f"Started: {now.isoformat()}")
    print(f"Quarter: {current_quarter_label(now.date())} ({start_date})")
    print("=" * 56)

    payload = scrape_all(start_date=start_date)
    payload["last_updated"] = datetime.now(BERLIN).isoformat()

    valid, success_rate = validate(payload)
    total = payload.get("total_attempted", 0)
    success = payload.get("count", 0)
    failed = len(payload.get("errors", []))
    print(f"Validation: {success}/{total} succeeded; {failed} failed ({success_rate:.1%})")

    if not valid:
        print(
            f"NOT APPLIED: success rate is below {SUCCESS_THRESHOLD:.0%}; "
            "Firestore was not updated."
        )
        return 1

    normalized = normalize_payload(payload, now.date().isoformat())
    db = firestore.Client()
    imported = import_seed_history_once(db)
    backfilled = backfill_category_summaries_once(db)
    preserve_last_known_site_update(db, normalized)
    snapshot_date = save_snapshot(db, normalized)

    exhausted = sum(
        1 for record in normalized["records"] if record.get("is_exhausted")
    )
    print(f"Seed snapshots imported: {imported}")
    print(f"Category summaries backfilled: {backfilled}")
    print(f"Snapshot saved: quota_snapshots/{snapshot_date}")
    print(f"Records: {success}; exhausted: {exhausted}")
    print(f"EU site update: {normalized.get('site_last_update') or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

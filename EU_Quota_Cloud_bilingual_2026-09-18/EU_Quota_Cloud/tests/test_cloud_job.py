import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_job


class FakeSnapshot:
    def __init__(self, exists=False, value=None):
        self.exists = exists
        self.value = value

    def to_dict(self):
        return self.value


class FakeDocument:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def get(self):
        return FakeSnapshot(self.path in self.store, self.store.get(self.path))

    def set(self, value):
        self.store[self.path] = value


class FakeQueryDoc:
    def __init__(self, doc_id, value):
        self.id = doc_id
        self._value = value

    def to_dict(self):
        return self._value


class FakeCollection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, document_id):
        return FakeDocument(self.store, f"{self.name}/{document_id}")

    def stream(self):
        prefix = f"{self.name}/"
        for path, value in list(self.store.items()):
            if path.startswith(prefix):
                yield FakeQueryDoc(path[len(prefix):], value)


class FakeBatch:
    def __init__(self):
        self.operations = []

    def set(self, document, value):
        self.operations.append((document, value))

    def commit(self):
        for document, value in self.operations:
            document.set(value)


class FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store, name)

    def batch(self):
        return FakeBatch()


class CloudJobTest(unittest.TestCase):
    def test_normalize_payload_uses_dashboard_schema(self):
        payload = {
            "last_updated": "2026-09-09T09:00:00",
            "tariff_quota_last_updated": "2026-09-08",
            "records": [],
        }
        result = cloud_job.normalize_payload(payload)
        self.assertEqual(result["snapshot_date"], "2026-09-09")
        self.assertEqual(result["site_last_update"], "2026-09-08")
        self.assertNotIn("tariff_quota_last_updated", result)
        self.assertIn("fta_origins", result)

    def test_validate_preserves_ninety_percent_safety_rule(self):
        self.assertEqual(cloud_job.validate({"count": 90, "total_attempted": 100}), (True, 0.9))
        self.assertEqual(cloud_job.validate({"count": 89, "total_attempted": 100}), (False, 0.89))

    def test_seed_history_imports_once(self):
        database = FakeFirestore()
        with tempfile.TemporaryDirectory() as folder:
            seed = Path(folder) / "2026-09-09.json"
            seed.write_text(
                json.dumps({"last_updated": "2026-09-09T09:00:00", "records": []}),
                encoding="utf-8",
            )
            with patch.object(cloud_job, "SEED_HISTORY_DIR", Path(folder)):
                self.assertEqual(cloud_job.import_seed_history_once(database), 1)
                self.assertEqual(cloud_job.import_seed_history_once(database), 0)

        self.assertIn("quota_snapshots/2026-09-09", database.store)
        self.assertIn("quota_meta/seed_history", database.store)

    def test_bundled_history_is_complete_and_below_firestore_limit(self):
        history_files = sorted(cloud_job.SEED_HISTORY_DIR.glob("*.json"))
        self.assertEqual(len(history_files), 8)
        for history_file in history_files:
            payload = cloud_job.normalize_payload(
                cloud_job.load_json(history_file), history_file.stem
            )
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.assertLess(len(encoded), 1_000_000)
            self.assertEqual(payload["snapshot_date"], history_file.stem)
            self.assertGreaterEqual(len(payload["records"]), 100)

    def test_save_snapshot_updates_snapshot_and_current_pointer(self):
        database = FakeFirestore()
        payload = {
            "snapshot_date": "2026-09-09",
            "last_updated": "2026-09-09T09:00:00",
            "site_last_update": "2026-09-08",
            "quarter_label": "2026 Q3",
            "records": [{"order_number": "099801"}],
            "errors": [],
            "total_attempted": 101,
            "count": 101,
            "failed_order_numbers": [],
        }
        result = cloud_job.save_snapshot(database, payload)
        self.assertEqual(result, "2026-09-09")
        self.assertEqual(database.store["quota_meta/current"]["latest_snapshot"], "2026-09-09")
        self.assertEqual(database.store["quota_meta/current"]["success_count"], 101)
        self.assertEqual(database.store["quota_meta/current"]["total_attempted"], 101)
        self.assertEqual(database.store["quota_meta/current"]["failed_order_numbers"], [])

    def test_missing_site_update_is_carried_forward(self):
        database = FakeFirestore()
        database.store["quota_meta/current"] = {"site_last_update": "2026-09-08"}
        payload = {"site_last_update": None}
        result = cloud_job.preserve_last_known_site_update(database, payload)
        self.assertEqual(result, "2026-09-08")
        self.assertEqual(payload["site_last_update"], "2026-09-08")

    def test_build_category_summary_sums_by_category(self):
        payload = {
            "snapshot_date": "2026-09-09",
            "quarter_label": "2026 Q3",
            "records": [
                {"category": "1A", "item_name": "열연강판", "amount_kg": 1000, "actual_remaining_kg": 400, "pending_kg": 10, "transferred_kg": 0},
                {"category": "1A", "item_name": "열연강판", "amount_kg": 500, "actual_remaining_kg": 100, "pending_kg": 0, "transferred_kg": 50},
                {"category": "2", "item_name": "냉연", "quarterly_kg": 200, "actual_remaining_kg": 200, "pending_kg": 0, "transferred_kg": 0},
            ],
        }
        summary = cloud_job.build_category_summary(payload)
        self.assertEqual(summary["date"], "2026-09-09")
        self.assertEqual(summary["quarter_label"], "2026 Q3")
        self.assertEqual(summary["success_count"], 0)
        self.assertEqual(summary["total_attempted"], 0)
        self.assertEqual(summary["failed_order_numbers"], [])
        self.assertEqual(summary["categories"]["1A"], {
            "item_name": "열연강판", "amount_kg": 1500, "actual_remaining_kg": 500,
            "pending_kg": 10, "transferred_kg": 50,
        })
        # falls back to quarterly_kg when amount_kg is absent (pre-carry-over records)
        self.assertEqual(summary["categories"]["2"]["amount_kg"], 200)

    def test_category_summary_keeps_collection_completeness(self):
        summary = cloud_job.build_category_summary({
            "snapshot_date": "2026-09-09",
            "quarter_label": "2026 Q3",
            "records": [],
            "count": 99,
            "total_attempted": 101,
            "failed_order_numbers": ["099801", "099802"],
        })
        self.assertEqual(summary["success_count"], 99)
        self.assertEqual(summary["total_attempted"], 101)
        self.assertEqual(summary["failed_order_numbers"], ["099801", "099802"])

    def test_save_snapshot_also_writes_category_summary(self):
        database = FakeFirestore()
        payload = {
            "snapshot_date": "2026-09-09", "quarter_label": "2026 Q3",
            "records": [{"category": "1A", "item_name": "열연강판", "amount_kg": 1000, "actual_remaining_kg": 400, "pending_kg": 0, "transferred_kg": 0}],
            "errors": [], "total_attempted": 1, "count": 1, "failed_order_numbers": [],
        }
        cloud_job.save_snapshot(database, payload)
        self.assertIn("quota_category_summary/2026-09-09", database.store)
        self.assertEqual(database.store["quota_category_summary/2026-09-09"]["categories"]["1A"]["amount_kg"], 1000)

    def test_backfill_category_summaries_v2_refreshes_legacy_documents_once(self):
        database = FakeFirestore()
        database.store["quota_snapshots/2026-09-08"] = {
            "snapshot_date": "2026-09-08", "quarter_label": "2026 Q3",
            "records": [{"category": "1A", "amount_kg": 100, "actual_remaining_kg": 50, "pending_kg": 0, "transferred_kg": 0}],
        }
        database.store["quota_snapshots/2026-09-09"] = {
            "snapshot_date": "2026-09-09", "quarter_label": "2026 Q3",
            "records": [{"category": "1A", "amount_kg": 100, "actual_remaining_kg": 40, "pending_kg": 0, "transferred_kg": 0}],
        }
        # Legacy summaries lack completeness metadata and must be refreshed by v2.
        database.store["quota_category_summary/2026-09-09"] = {"categories": {"1A": {"amount_kg": 999}}}

        self.assertEqual(cloud_job.backfill_category_summaries_once(database), 2)
        self.assertEqual(database.store["quota_category_summary/2026-09-08"]["categories"]["1A"]["amount_kg"], 100)
        self.assertEqual(database.store["quota_category_summary/2026-09-09"]["categories"]["1A"]["amount_kg"], 100)
        self.assertIn("success_count", database.store["quota_category_summary/2026-09-09"])
        self.assertIn("quota_meta/category_summary_backfill", database.store)
        self.assertEqual(database.store["quota_meta/category_summary_backfill"]["version"], 2)
        self.assertEqual(cloud_job.backfill_category_summaries_once(database), 0)


if __name__ == "__main__":
    unittest.main()

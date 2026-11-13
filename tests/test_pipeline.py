import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from accounting.pipeline import TABLES, ValidationError, collect, load, money, profile, validate, write_json


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.output = self.root / "reports"
        self.tables = {
            "companies": [{"id": "c"}],
            "chart_of_accounts": [{"id": "a", "company_id": "c", "account_type": "asset"},
                                  {"id": "b", "company_id": "c", "account_type": "equity"}],
            "journal_entries": [{"id": "j", "company_id": "c", "entry_date": "2024-01-01"}],
            "journal_entry_lines": [
                {"id": "l1", "journal_entry_id": "j", "account_id": "a", "debit": "0.30", "credit": "0"},
                {"id": "l2", "journal_entry_id": "j", "account_id": "b", "debit": "0", "credit": "0.30"}],
            "trial_balances": [{"id": "t"}],
        }

    def save(self):
        manifest = {"files": {}}
        for table in TABLES:
            path = self.snapshot / f"{table}.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(self.tables[table][0]))
                writer.writeheader()
                writer.writerows(self.tables[table])
            manifest["files"][table] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        write_json(self.snapshot / "manifest.json", manifest)

    def failures(self):
        self.save()
        with self.assertRaises(ValidationError):
            validate(self.snapshot, self.output)
        return json.loads((self.output / "validation_summary.json").read_text())["error_counts"]

    def test_balanced_and_repeatable(self):
        self.tables["journal_entry_lines"][0]["debit"] = "0.10"
        self.tables["journal_entry_lines"].append(
            {"id": "l3", "journal_entry_id": "j", "account_id": "a", "debit": "0.20", "credit": "0"})
        self.save()
        validate(self.snapshot, self.output)
        before = (self.output / "validation_summary.json").read_bytes()
        validate(self.snapshot, self.output)
        self.assertEqual(before, (self.output / "validation_summary.json").read_bytes())
        profile(self.snapshot, self.output)
        report = json.loads((self.output / "profile.json").read_text())
        self.assertEqual(report["tables"]["journal_entries"]["columns"]["entry_date"]["min"], "2024-01-01")
        self.assertEqual(report["tables"]["journal_entry_lines"]["columns"]["debit"]["total"], "0.30")

    def test_invalid_date_and_zero_line(self):
        self.tables["journal_entries"][0]["entry_date"] = "2024-02-30"
        self.tables["journal_entry_lines"][0]["debit"] = "0"
        errors = self.failures()
        self.assertIn("invalid_entry_date", errors)
        self.assertIn("invalid_debit_credit_side", errors)

    def test_offsetting_unbalanced_entries_are_not_hidden(self):
        self.tables["journal_entries"].append({"id": "j2", "company_id": "c", "entry_date": "2024-01-02"})
        self.tables["journal_entry_lines"][1]["credit"] = "0.20"
        self.tables["journal_entry_lines"] += [
            {"id": "l3", "journal_entry_id": "j2", "account_id": "a", "debit": "0.20", "credit": "0"},
            {"id": "l4", "journal_entry_id": "j2", "account_id": "b", "debit": "0", "credit": "0.30"}]
        self.assertEqual(self.failures()["unbalanced_entry"], 2)

    def test_invalid_money(self):
        for value in ("", "NaN", "Infinity", "-1", "0.001", "abc", "1e100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        self.tables["journal_entry_lines"][0]["debit"] = "NaN"
        self.assertEqual(self.failures()["invalid_amount"], 1)

    def test_reference_duplicate_and_company_errors(self):
        self.tables["journal_entry_lines"][0]["account_id"] = "missing"
        self.tables["journal_entry_lines"][1]["journal_entry_id"] = "missing"
        self.tables["journal_entries"].append(dict(self.tables["journal_entries"][0]))
        self.tables["chart_of_accounts"][0]["company_id"] = "other"
        errors = self.failures()
        for code in ("unknown_account", "unknown_journal_entry", "duplicate_id", "unknown_company", "insufficient_lines"):
            self.assertIn(code, errors)

    def test_missing_lines_and_both_sides(self):
        self.tables["journal_entries"].append({"id": "empty", "company_id": "c", "entry_date": "2024-01-01"})
        self.tables["journal_entry_lines"][0]["credit"] = "0.10"
        errors = self.failures()
        self.assertIn("insufficient_lines", errors)
        self.assertIn("invalid_debit_credit_side", errors)

    def test_company_mismatch(self):
        self.tables["companies"].append({"id": "other"})
        self.tables["chart_of_accounts"][0]["company_id"] = "other"
        self.assertEqual(self.failures()["company_mismatch"], 1)

    def test_integrity_and_schema(self):
        self.save()
        with (self.snapshot / "companies.csv").open("a") as f:
            f.write("corrupt\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load(self.snapshot)
        self.tables["journal_entry_lines"][0].pop("debit")
        self.tables["journal_entry_lines"][1].pop("debit")
        self.save()
        with self.assertRaisesRegex(ValueError, "Missing columns"):
            load(self.snapshot)

    def test_collect_atomic_manifest_and_cache(self):
        payload = b"id\nx\n"
        from io import BytesIO
        with patch("accounting.pipeline.urlopen", side_effect=lambda *a, **k: BytesIO(payload)) as request:
            snapshot = collect(self.root)
            self.assertEqual(request.call_count, 5)
            collect(self.root)
            self.assertEqual(request.call_count, 5)
        manifest = json.loads((Path(snapshot) / "manifest.json").read_text())
        self.assertEqual(set(manifest["files"]), set(TABLES))
        with patch("accounting.pipeline.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                collect(self.root, "a" * 40)
        self.assertFalse((self.root / "raw" / ("a" * 40) / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()

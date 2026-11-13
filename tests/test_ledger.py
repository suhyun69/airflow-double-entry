import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from accounting.ledger import build_general_ledger, build_trial_balance, reconcile_trial_balance
from accounting.pipeline import ValidationError, write_json


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snapshot = Path(self.temp.name) / "snapshot"
        self.snapshot.mkdir()
        self.output = Path(self.temp.name) / "reports"
        self.tables = {
            "companies": [{"id": "c"}, {"id": "other"}],
            "chart_of_accounts": [], "journal_entries": [], "journal_entry_lines": [], "trial_balances": []}
        for company in ("c", "other"):
            for code, kind in (("cash", "asset"), ("capital", "equity"), ("expense", "expense"), ("unused", "asset")):
                self.tables["chart_of_accounts"].append({"id": f"{company}-{code}", "company_id": company,
                    "code": code, "name": code, "account_type": kind})
        self.entry("opening", "2024-01-01", "cash", "capital", "100", number=1)
        self.entry("expense", "2024-01-31", "expense", "cash", "20", number=2)
        self.entry("reversal", "2024-03-05", "cash", "expense", "5", number=10)
        self.entry("draft", "2024-02-01", "cash", "capital", "999", posted="False", number=3)
        self.entry("other-opening", "2024-01-01", "cash", "capital", "7", company="other", number=1)
        for period in ("2024-01-31", "2024-02-29", "2024-03-31"):
            march = period[5:7] == "03"
            self.source(period, "cash", "105" if march else "100", "20")
            self.source(period, "capital", "0", "100")
            self.source(period, "expense", "20", "5" if march else "0")
        self.source("2024-01-31", "cash", "7", "0", company="other")
        self.source("2024-01-31", "capital", "0", "7", company="other")

    def entry(self, ident, day, debit_account, credit_account, value, company="c", posted="True", number=1):
        self.tables["journal_entries"].append({"id": ident, "company_id": company,
            "entry_date": day, "entry_number": str(number), "posted": posted})
        for side, account in (("debit", debit_account), ("credit", credit_account)):
            self.tables["journal_entry_lines"].append({"id": f"{ident}-{side}", "journal_entry_id": ident,
                "account_id": f"{company}-{account}", "description": ident,
                "debit": value if side == "debit" else "0", "credit": value if side == "credit" else "0"})

    def source(self, period, account, debit, credit, company="c"):
        rows = self.tables["trial_balances"]
        rows.append({"id": f"s{len(rows)}", "company_id": company, "period_end": period,
            "account_id": f"{company}-{account}", "debit_balance": debit, "credit_balance": credit})

    def save(self):
        manifest = {"files": {}}
        for table, rows in self.tables.items():
            path = self.snapshot / f"{table}.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            manifest["files"][table] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        write_json(self.snapshot / "manifest.json", manifest)

    def build(self):
        self.save()
        build_general_ledger(self.snapshot, self.output)
        build_trial_balance(self.snapshot, self.output)

    def read(self, name):
        if name.endswith(".json"):
            return json.loads((self.output / name).read_text())
        with (self.output / name).open(newline="") as f:
            return list(csv.DictReader(f))

    def test_known_balances_empty_month_posted_only_and_company_isolation(self):
        self.build()
        ledger = self.read("general_ledger.csv")
        self.assertEqual(len(ledger), 8)
        self.assertFalse(any(r["journal_entry_id"] == "draft" for r in ledger))
        cash = [r for r in ledger if r["account_id"] == "c-cash"]
        self.assertEqual([r["running_balance"] for r in cash], ["100.00", "80.00", "85.00"])
        rows = {(r["company_id"], r["period_end"], r["account_id"]): r for r in self.read("monthly_trial_balance.csv")}
        feb = rows["c", "2024-02-29", "c-cash"]
        self.assertEqual((feb["opening_balance"], feb["period_debit"], feb["period_credit"], feb["closing_balance"]),
                         ("80.00", "0.00", "0.00", "80.00"))
        march = rows["c", "2024-03-31", "c-expense"]
        self.assertEqual((march["opening_balance"], march["period_credit"], march["debit_balance"], march["credit_balance"]),
                         ("20.00", "5.00", "15.00", "0.00"))
        self.assertEqual(rows["other", "2024-01-31", "other-cash"]["closing_balance"], "7.00")
        reconcile_trial_balance(self.snapshot, self.output)
        self.assertEqual(self.read("reconciliation_summary.json")["status"], "passed")

    def test_rerun_and_input_order_do_not_change_outputs(self):
        self.build()
        before = (self.output / "general_ledger.csv").read_bytes(), (self.output / "monthly_trial_balance.csv").read_bytes()
        for rows in self.tables.values():
            rows.reverse()
        self.build()
        self.assertEqual(before, ((self.output / "general_ledger.csv").read_bytes(),
                                  (self.output / "monthly_trial_balance.csv").read_bytes()))

    def test_numeric_entry_order_and_year_boundary(self):
        self.entry("late", "2024-01-01", "cash", "capital", "2", number=10)
        self.entry("early", "2024-01-01", "cash", "capital", "3", number=2)
        self.entry("next-year", "2025-01-01", "cash", "capital", "1", number=11)
        self.build()
        cash = [r for r in self.read("general_ledger.csv") if r["account_id"] == "c-cash"]
        self.assertEqual([r["entry_number"] for r in cash[:3]], ["1", "2", "10"])
        january = next(r for r in self.read("monthly_trial_balance.csv") if r["account_id"] == "c-cash" and r["period_end"] == "2025-01-31")
        self.assertEqual((january["opening_balance"], january["closing_balance"]), ("90.00", "91.00"))

    def test_mismatch_is_not_hidden_by_equal_net(self):
        self.tables["trial_balances"][0].update(debit_balance="101", credit_balance="21")
        self.build()
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.snapshot, self.output)
        row = next(r for r in self.read("reconciliation_details.json") if r["status"] == "amount_mismatch")
        self.assertEqual((row["debit_difference"], row["credit_difference"], row["net_difference"]), ("-1.00", "-1.00", "0.00"))

    def test_missing_nonzero_and_missing_period_fail(self):
        self.tables["trial_balances"] = [r for r in self.tables["trial_balances"] if r["period_end"] != "2024-02-29"]
        self.build()
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.snapshot, self.output)
        self.assertEqual(self.read("reconciliation_summary.json")["counts"]["missing_source"], 3)

    def test_zero_net_with_nonzero_gross_cannot_be_omitted(self):
        self.entry("reverse-all", "2024-01-02", "capital", "cash", "100", number=4)
        self.tables["trial_balances"] = [r for r in self.tables["trial_balances"] if not (r["account_id"] == "c-capital" and r["period_end"] == "2024-01-31")]
        self.build()
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.snapshot, self.output)
        self.assertEqual(self.read("reconciliation_summary.json")["counts"]["missing_source"], 1)

    def test_source_extra_period_not_silently_ignored(self):
        self.source("2024-04-30", "cash", "105", "20")
        self.build()
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.snapshot, self.output)
        self.assertEqual(self.read("reconciliation_summary.json")["counts"]["missing_calculated"], 1)

    def test_source_natural_key_duplicate_and_invalid_rows(self):
        self.source("2024-01-31", "cash", "100", "20")
        self.source("2024-02-28", "cash", "0", "0")
        self.source("2024-01-31", "nonexistent", "0", "0")
        self.source("2024-03-31", "unused", "NaN", "0")
        self.build()
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.snapshot, self.output)
        self.assertEqual(self.read("reconciliation_summary.json")["source_error_count"], 4)

    def test_month_end_evidence_never_corrects_result(self):
        # Original source omits this month's final expense journal.
        self.tables["trial_balances"][0]["credit_balance"] = "0"
        self.tables["trial_balances"] = [r for r in self.tables["trial_balances"] if not (r["period_end"] == "2024-01-31" and r["account_id"] == "c-expense")]
        self.build()
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.snapshot, self.output)
        report = self.read("reconciliation_summary.json")
        self.assertEqual((report["difference_count"], report["month_end_explained_count"]), (2, 2))
        rows = [r for r in self.read("reconciliation_details.json") if r.get("month_end_lines_explain_difference")]
        self.assertTrue(all(r["candidate_descriptions"] == ["expense"] for r in rows))

    def test_artifact_and_source_provenance(self):
        self.build()
        with (self.output / "general_ledger.csv").open("a") as f:
            f.write("corrupt")
        with self.assertRaisesRegex(ValueError, "Checksum"):
            build_trial_balance(self.snapshot, self.output)
        self.build()
        self.entry("new", "2024-01-03", "cash", "capital", "1", number=4)
        self.save()
        with self.assertRaisesRegex(ValueError, "Stale"):
            build_trial_balance(self.snapshot, self.output)

    def test_bad_posted_and_unbalanced_inputs_block_ledger(self):
        self.tables["journal_entries"][0]["posted"] = "yes"
        self.save()
        with self.assertRaisesRegex(ValidationError, "posted"):
            build_general_ledger(self.snapshot, self.output)
        self.tables["journal_entries"][0]["posted"] = "True"
        self.tables["journal_entry_lines"][0]["debit"] = "99"
        self.save()
        with self.assertRaises(ValidationError):
            build_general_ledger(self.snapshot, self.output)
        self.assertFalse((self.output / "general_ledger.csv").exists())


if __name__ == "__main__":
    unittest.main()

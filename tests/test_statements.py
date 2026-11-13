import json
import unittest

import test_ledger as fixtures
from accounting.ledger import receipt, write_csv
from accounting.pipeline import ValidationError
from accounting.statements import (IS_FIELDS, BS_FIELDS, build_statements, validate_statements)


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LedgerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.output = self.f.output
        for company in self.f.tables["companies"]:
            company["financial_year_end_month"] = "12"
        for account in self.f.tables["chart_of_accounts"]:
            account["sub_type"] = {"asset": "current_asset", "expense": "operating_expense", "equity": "equity"}[account["account_type"]]
        self.mapping_path = self.f.snapshot.parent / "mapping.json"

    def add_account(self, code, kind, subtype):
        self.f.tables["chart_of_accounts"].append({"id": f"c-{code}", "company_id": "c", "code": code,
            "name": code, "account_type": kind, "sub_type": subtype})

    def mapping(self):
        sections = {"current_asset": "current_assets", "fixed_asset": "noncurrent_assets",
            "operating_expense": "operating_expense", "equity": "recorded_equity",
            "operating_revenue": "operating_revenue", "other_revenue": "other_revenue",
            "cogs": "cost_of_goods_sold", "current_liability": "current_liabilities", "non_current_liability": "noncurrent_liabilities"}
        result = {"version": 1, "accounts": {a["code"]: {"account_type": a["account_type"],
            "sub_type": a["sub_type"], "section": sections[a["sub_type"]],
            "earnings_transfer": a["code"] == "retained",
            "cash_flow_activity": 'cash' if a['code'] == 'cash' else 'investing' if a['sub_type'] == 'fixed_asset' else 'financing' if a['account_type'] == 'equity' or a['code'] == 'loan' else 'operating'} for a in self.f.tables["chart_of_accounts"]}}
        self.mapping_path.write_text(json.dumps(result))
        return result

    def build(self):
        self.mapping()
        self.f.build()
        build_statements(self.f.snapshot, self.output, self.mapping_path)

    def test_hand_calculated_monthly_ytd_losses_and_company_isolation(self):
        self.build()
        validate_statements(self.f.snapshot, self.output)
        income = {(r["company_id"], r["period_end"], r["basis"]): r for r in self.f.read("income_statement.csv")}
        self.assertEqual(income["c", "2024-01-31", "monthly"]["net_income"], "-20.00")
        self.assertEqual(income["c", "2024-02-29", "monthly"]["net_income"], "0.00")
        self.assertEqual(income["c", "2024-03-31", "monthly"]["net_income"], "5.00")
        self.assertEqual(income["c", "2024-03-31", "fiscal_ytd"]["net_income"], "-15.00")
        bs = {(r["company_id"], r["period_end"]): r for r in self.f.read("balance_sheet.csv")}
        self.assertEqual((bs["c", "2024-03-31"]["total_assets"], bs["c", "2024-03-31"]["total_equity"]), ("85.00", "85.00"))
        self.assertEqual(bs["other", "2024-01-31"]["total_equity"], "7.00")

    def test_contra_accounts_returns_draws_liabilities_and_profit_subtotals(self):
        for args in (("sales", "revenue", "operating_revenue"), ("equipment", "asset", "fixed_asset"),
                     ("depreciation", "asset", "fixed_asset"), ("draws", "equity", "equity"),
                     ("payable", "liability", "current_liability"), ("cost", "expense", "cogs"),
                     ("interest", "revenue", "other_revenue")):
            self.add_account(*args)
        self.f.entry("sale", "2024-01-15", "cash", "sales", "50", number=3)
        self.f.entry("return", "2024-01-16", "sales", "cash", "10", number=4)
        self.f.entry("equipment", "2024-01-18", "equipment", "cash", "30", number=5)
        self.f.entry("depreciation", "2024-01-31", "expense", "depreciation", "3", number=6)
        self.f.entry("draw", "2024-01-20", "draws", "cash", "7", number=7)
        self.f.entry("cogs", "2024-01-21", "cost", "payable", "8", number=8)
        self.f.entry("interest", "2024-01-22", "cash", "interest", "2", number=9)
        self.build()
        validate_statements(self.f.snapshot, self.output)
        inc = next(r for r in self.f.read("income_statement.csv") if r["company_id"] == "c" and r["period_end"] == "2024-01-31" and r["basis"] == "monthly")
        self.assertEqual(tuple(inc[k] for k in ("operating_revenue", "gross_profit", "operating_profit", "net_income")), ("40.00", "32.00", "9.00", "11.00"))
        bs = next(r for r in self.f.read("balance_sheet.csv") if r["company_id"] == "c" and r["period_end"] == "2024-01-31")
        self.assertEqual(tuple(bs[k] for k in ("noncurrent_assets", "total_assets", "total_liabilities", "recorded_equity", "total_equity")), ("27.00", "112.00", "8.00", "93.00", "104.00"))
        detail = next(r for r in self.f.read("statement_accounts.csv") if r["account_id"] == "c-depreciation" and r["period_end"] == "2024-01-31")
        self.assertEqual(detail["closing_amount"], "-3.00")

    def test_year_boundary_carries_prior_profit_without_double_counting(self):
        self.add_account("retained", "equity", "equity")
        self.f.entry("initial-retained", "2024-01-01", "cash", "retained", "40", number=2)
        self.f.entry("new-year-expense", "2025-01-01", "expense", "cash", "2", number=11)
        self.build()
        validate_statements(self.f.snapshot, self.output)
        bs = next(r for r in self.f.read("balance_sheet.csv") if r["company_id"] == "c" and r["period_end"] == "2025-01-31")
        self.assertEqual(tuple(bs[k] for k in ("recorded_equity", "prior_unclosed_earnings", "current_fiscal_year_profit", "total_equity")), ("140.00", "-15.00", "-2.00", "123.00"))

    def test_non_calendar_fiscal_year(self):
        self.f.tables["companies"][0]["financial_year_end_month"] = "2"
        self.build()
        validate_statements(self.f.snapshot, self.output)
        bs = next(r for r in self.f.read("balance_sheet.csv") if r["company_id"] == "c" and r["period_end"] == "2024-03-31")
        self.assertEqual((bs["fiscal_year_start"], bs["prior_unclosed_earnings"], bs["current_fiscal_year_profit"]), ("2024-03-01", "-20.00", "5.00"))

    def test_unknown_account_even_zero_balance_fails(self):
        self.build()
        mapping = self.mapping()
        mapping["accounts"].pop("unused")
        self.mapping_path.write_text(json.dumps(mapping))
        with self.assertRaisesRegex(ValidationError, "Unmapped"):
            build_statements(self.f.snapshot, self.output, self.mapping_path)
        self.assertEqual(self.f.read("statements_summary.json")["status"], "failed")
        with self.assertRaisesRegex(ValidationError, "did not complete"):
            validate_statements(self.f.snapshot, self.output)
        self.assertEqual(self.f.read("statement_validation_summary.json")["status"], "failed")

    def test_negative_source_assets_preserved_and_reported(self):
        self.add_account("depreciation", "asset", "fixed_asset")
        self.f.entry("depreciation", "2024-01-31", "expense", "depreciation", "3", number=4)
        self.build()
        validate_statements(self.f.snapshot, self.output)
        observations = self.f.read("statement_observations.json")
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(r["section"] == "noncurrent_assets" and r["amount"] == "-3.00" for r in observations))

    def test_invalid_mapping_and_fiscal_settings(self):
        self.build()
        mapping = self.mapping()
        mapping["accounts"]["expense"]["section"] = "current_assets"
        self.mapping_path.write_text(json.dumps(mapping))
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            build_statements(self.f.snapshot, self.output, self.mapping_path)
        self.f.tables["companies"][0]["financial_year_end_month"] = "13"
        self.f.save()
        self.mapping()
        with self.assertRaisesRegex(ValidationError, "fiscal"):
            build_statements(self.f.snapshot, self.output, self.mapping_path)

    def test_profit_closing_not_silently_counted_as_expense(self):
        self.add_account("retained", "equity", "equity")
        self.f.entry("close", "2024-03-31", "retained", "expense", "15", number=20)
        self.mapping()
        self.f.build()
        with self.assertRaisesRegex(ValidationError, "closing"):
            build_statements(self.f.snapshot, self.output, self.mapping_path)

    def test_independent_crosscheck_detects_semantic_corruption(self):
        self.build()
        rows = self.f.read("income_statement.csv")
        rows[0]["operating_expense"] = "999.00"
        write_csv(self.output / "income_statement.csv", IS_FIELDS, rows)
        # Renew the receipt to exercise calculation checks rather than byte checks.
        receipt(self.f.snapshot, self.output, "income_statement", "income_statement.csv")
        with self.assertRaisesRegex(ValidationError, "cross-check"):
            validate_statements(self.f.snapshot, self.output)
        self.assertGreater(self.f.read("statement_validation_summary.json")["failed_check_count"], 0)

    def test_balance_sheet_corruption_and_period_coverage(self):
        self.build()
        rows = self.f.read("balance_sheet.csv")
        rows[0]["total_assets"] = "999.00"
        write_csv(self.output / "balance_sheet.csv", BS_FIELDS, rows)
        receipt(self.f.snapshot, self.output, "balance_sheet", "balance_sheet.csv")
        with self.assertRaisesRegex(ValidationError, "cross-check"):
            validate_statements(self.f.snapshot, self.output)
        rows.pop()
        write_csv(self.output / "balance_sheet.csv", BS_FIELDS, rows)
        receipt(self.f.snapshot, self.output, "balance_sheet", "balance_sheet.csv")
        with self.assertRaisesRegex(ValidationError, "coverage"):
            validate_statements(self.f.snapshot, self.output)

    def test_artifact_mapping_integrity_and_repeatability(self):
        self.build()
        before = (self.output / "income_statement.csv").read_bytes(), (self.output / "balance_sheet.csv").read_bytes()
        build_statements(self.f.snapshot, self.output, self.mapping_path)
        self.assertEqual(before, ((self.output / "income_statement.csv").read_bytes(), (self.output / "balance_sheet.csv").read_bytes()))
        (self.output / "statement_mapping.json").write_text('{}')
        with self.assertRaisesRegex(ValidationError, "mapping checksum"):
            validate_statements(self.f.snapshot, self.output)

"""Draft monthly/fiscal-YTD statements and independent ledger cross-checks."""
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from accounting.ledger import ZERO, amount, read_artifact, receipt, require, write_csv
from accounting.pipeline import ValidationError, load, write_json

SECTIONS = {
    "current_assets": "asset", "noncurrent_assets": "asset",
    "current_liabilities": "liability", "noncurrent_liabilities": "liability",
    "recorded_equity": "equity", "operating_revenue": "revenue", "other_revenue": "revenue",
    "cost_of_goods_sold": "expense", "operating_expense": "expense"}
PL = {"revenue", "expense"}
INCOME_COMPONENTS = ["operating_revenue", "cost_of_goods_sold", "operating_expense", "other_revenue"]
BS_COMPONENTS = ["current_assets", "noncurrent_assets", "current_liabilities", "noncurrent_liabilities", "recorded_equity"]
IS_FIELDS = ["company_id", "period_start", "period_end", "basis", "fiscal_year_start",
             "operating_revenue", "cost_of_goods_sold", "gross_profit", "operating_expense",
             "operating_profit", "other_revenue", "net_income"]
BS_FIELDS = ["company_id", "period_end", "fiscal_year_start", "cash_and_cash_equivalents", "current_assets", "noncurrent_assets", "total_assets",
             "current_liabilities", "noncurrent_liabilities", "total_liabilities", "recorded_equity",
             "prior_unclosed_earnings", "current_fiscal_year_profit", "total_equity", "total_liabilities_and_equity"]
DETAIL_FIELDS = ["company_id", "period_end", "account_id", "account_code", "account_name", "section",
                 "account_type", "monthly_amount", "fiscal_ytd_amount", "closing_amount"]


def fiscal_start(day, end_month):
    month = end_month % 12 + 1
    return date(day.year if day.month >= month else day.year - 1, month, 1)


def signed(value, kind):
    # Source is debit-positive; contra accounts keep their actual opposite sign.
    return -value if kind in {"revenue", "liability", "equity"} else value


def net(values):
    return values["operating_revenue"] - values["cost_of_goods_sold"] - values["operating_expense"] + values["other_revenue"]


def configuration(tables, columns, mapping_path=None):
    require(columns, "companies", ["financial_year_end_month"])
    require(columns, "chart_of_accounts", ["code", "name", "sub_type"])
    mapping = json.loads(Path(mapping_path or Path(__file__).with_name("statement_mapping.json")).read_text())
    if mapping.get("version") != 1:
        raise ValidationError("Unsupported statement mapping version")
    end_months = {}
    for company in tables["companies"]:
        try:
            value = int(company["financial_year_end_month"])
            if not 1 <= value <= 12:
                raise ValueError()
        except ValueError:
            raise ValidationError(f"Invalid fiscal year end: {company['id']}")
        end_months[company["id"]] = value
    specs, seen = {}, set()
    for account in tables["chart_of_accounts"]:
        code = account["code"]
        spec = mapping.get("accounts", {}).get(code)
        if (not spec or spec.get("account_type") != account["account_type"] or
                spec.get("sub_type") != account["sub_type"] or
                SECTIONS.get(spec.get("section")) != account["account_type"]):
            raise ValidationError(f"Unmapped or incompatible account: {code} ({account['id']})")
        if spec.get("earnings_transfer") and account["account_type"] != "equity":
            raise ValidationError(f"Invalid earnings transfer mapping: {code}")
        key = account["company_id"], code
        if key in seen:
            raise ValidationError(f"Duplicate account code within company: {key}")
        seen.add(key)
        specs[account["id"]] = {**spec, "company_id": account["company_id"]}
    return mapping, end_months, specs


def reject_closings(ledger, specs):
    entries = defaultdict(set)
    for line in ledger:
        spec = specs[line["account_id"]]
        if spec["account_type"] in PL:
            entries[line["journal_entry_id"]].add("pl")
        if spec.get("earnings_transfer"):
            entries[line["journal_entry_id"]].add("earnings")
    ambiguous = [entry for entry, kinds in entries.items() if kinds == {"pl", "earnings"}]
    if ambiguous:
        raise ValidationError(f"Unsupported profit closing/earnings transfer journals: {ambiguous}; explicit closing classification required")


def build_statements(snapshot, output, mapping_path=None):
    output = Path(output)
    write_json(output / "statements_summary.json", {"status": "building"})
    try:
        return _build_statements(snapshot, output, mapping_path)
    except Exception as exc:
        write_json(output / "statements_summary.json", {"status": "failed", "error": str(exc)})
        raise


def _build_statements(snapshot, output, mapping_path=None):
    tables, columns = load(snapshot)
    mapping, end_months, specs = configuration(tables, columns, mapping_path)
    trial = read_artifact(snapshot, output, "trial_balance")
    ledger = read_artifact(snapshot, output, "ledger")
    reject_closings(ledger, specs)
    accounts = {a["id"]: a for a in tables["chart_of_accounts"]}
    groups = defaultdict(list)
    seen = set()
    for row in trial:
        key = row["company_id"], row["period_end"], row["account_id"]
        if key in seen or row["account_id"] not in specs or specs[row["account_id"]]["company_id"] != row["company_id"]:
            raise ValidationError(f"Invalid trial balance key: {key}")
        seen.add(key)
        groups[key[:2]].append(row)
    if not groups:
        raise ValidationError("No trial balance periods for statements")
    income, balance, details = [], [], []
    ytd = defaultdict(Decimal)
    with localcontext() as ctx:
        ctx.prec = 50
        for (company, period), rows in sorted(groups.items()):
            expected = {a["id"] for a in tables["chart_of_accounts"] if a["company_id"] == company}
            if {r["account_id"] for r in rows} != expected:
                raise ValidationError(f"Incomplete trial balance coverage: {company} {period}")
            day = date.fromisoformat(period)
            start = fiscal_start(day, end_months[company]).isoformat()
            monthly, yearly, closing = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
            lifetime_profit = ZERO
            cash_balance = ZERO
            for row in rows:
                acc = row["account_id"]
                spec, account = specs[acc], accounts[acc]
                kind, section = spec["account_type"], spec["section"]
                movement = Decimal(row["period_debit"]) - Decimal(row["period_credit"])
                value = Decimal(row["closing_balance"])
                if not movement.is_finite() or not value.is_finite():
                    raise ValidationError(f"Nonfinite trial balance value: {acc}")
                ytd[company, start, acc] += movement
                m, y, c = signed(movement, kind), signed(ytd[company, start, acc], kind), signed(value, kind)
                monthly[section] += m
                yearly[section] += y
                closing[section] += c
                if spec.get('cash_flow_activity') == 'cash':
                    cash_balance += value
                if kind in PL:
                    lifetime_profit -= value
                details.append(dict(zip(DETAIL_FIELDS, [company, period, acc, account["code"], account["name"], section,
                                                         kind, amount(m), amount(y), amount(c)])))
            for basis, period_start, values in (("monthly", day.replace(day=1).isoformat(), monthly), ("fiscal_ytd", start, yearly)):
                gross = values["operating_revenue"] - values["cost_of_goods_sold"]
                operating = gross - values["operating_expense"]
                income.append({"company_id": company, "period_start": period_start, "period_end": period,
                               "basis": basis, "fiscal_year_start": start,
                               **{k: amount(values[k]) for k in INCOME_COMPONENTS},
                               "gross_profit": amount(gross), "operating_profit": amount(operating), "net_income": amount(net(values))})
            current_profit = net(yearly)
            assets = closing["current_assets"] + closing["noncurrent_assets"]
            liabilities = closing["current_liabilities"] + closing["noncurrent_liabilities"]
            equity = closing["recorded_equity"] + lifetime_profit
            balance.append({"company_id": company, "period_end": period, "fiscal_year_start": start,
                            "cash_and_cash_equivalents": amount(cash_balance),
                            **{k: amount(closing[k]) for k in BS_COMPONENTS}, "total_assets": amount(assets),
                            "total_liabilities": amount(liabilities), "prior_unclosed_earnings": amount(lifetime_profit - current_profit),
                            "current_fiscal_year_profit": amount(current_profit), "total_equity": amount(equity),
                            "total_liabilities_and_equity": amount(liabilities + equity)})
    output = Path(output)
    write_json(output / "statement_mapping.json", mapping)
    for stage, filename, fields, rows in (
        ("income_statement", "income_statement.csv", IS_FIELDS, income),
        ("balance_sheet", "balance_sheet.csv", BS_FIELDS, balance),
        ("statement_accounts", "statement_accounts.csv", DETAIL_FIELDS, details)):
        write_csv(output / filename, fields, rows)
        receipt(snapshot, output, stage, filename, status="draft", row_count=len(rows))
    observations = [
        {"company_id": row["company_id"], "period_end": row["period_end"],
         "code": "negative_asset_section", "section": section, "amount": row[section],
         "note": "Preserved source balance; check missing acquisitions/opening balances before publication"}
        for row in balance for section in ("current_assets", "noncurrent_assets") if Decimal(row[section]) < ZERO]
    write_json(output / "statement_observations.json", observations)
    write_json(output / "statements_summary.json", {
        "status": "draft", "basis": "reconstructed posted journals; source reconciliation is a separate quality gate",
        "company_period_count": len(balance), "income_statement_row_count": len(income), "balance_sheet_row_count": len(balance),
        "mapped_account_count": len(specs), "unmapped_account_count": 0,
        "observation_count": len(observations),
        "mapping_sha256": hashlib.sha256((output / "statement_mapping.json").read_bytes()).hexdigest(),
        "trial_balance_sha256": hashlib.sha256((output / "monthly_trial_balance.csv").read_bytes()).hexdigest(),
        "earnings_policy": "recorded equity + prior unclosed earnings + current fiscal-YTD profit; no synthetic journal postings",
        "coverage": "first available source entry onward; opening balances and taxes are not inferred"})
    return str(output / "statements_summary.json")


def validate_statements(snapshot, output):
    output = Path(output)
    summary_path = output / "statement_validation_summary.json"
    write_json(summary_path, {"status": "running"})
    try:
        return _validate_statements(snapshot, output)
    except Exception as exc:
        if json.loads(summary_path.read_text())["status"] == "running":
            write_json(summary_path, {"status": "failed", "error": str(exc), "publication_status": "draft"})
        raise


def _validate_statements(snapshot, output):
    """Check generated statements against independently aggregated ledger lines."""
    output = Path(output)
    tables, columns = load(snapshot)
    summary = json.loads((output / "statements_summary.json").read_text())
    if summary.get("status") != "draft":
        raise ValidationError("Statement generation did not complete successfully")
    if hashlib.sha256((output / "statement_mapping.json").read_bytes()).hexdigest() != summary["mapping_sha256"]:
        raise ValidationError("Statement mapping checksum mismatch")
    if hashlib.sha256((output / "monthly_trial_balance.csv").read_bytes()).hexdigest() != summary["trial_balance_sha256"]:
        raise ValidationError("Trial balance changed after statement generation")
    _, end_months, specs = configuration(tables, columns, output / "statement_mapping.json")
    ledger = read_artifact(snapshot, output, "ledger")
    read_artifact(snapshot, output, "trial_balance")
    income = read_artifact(snapshot, output, "income_statement")
    balance = read_artifact(snapshot, output, "balance_sheet")
    details = read_artifact(snapshot, output, "statement_accounts")
    checks = []
    def check(company, period, name, actual, expected):
        difference = actual - expected
        checks.append({"company_id": company, "period_end": period, "check": name, "actual": amount(actual),
                       "expected": amount(expected), "difference": amount(difference), "passed": difference == ZERO})
    index = {}
    for row in income:
        key = row["company_id"], row["period_end"], row["basis"]
        if key in index:
            raise ValidationError(f"Duplicate income statement key: {key}")
        index[key] = row
    # Verify exact period coverage against the trial balance (not statement outputs).
    trial = read_artifact(snapshot, output, "trial_balance")
    periods = {(r["company_id"], r["period_end"]) for r in trial}
    bs_index = {(r["company_id"], r["period_end"]): r for r in balance}
    if len(bs_index) != len(balance) or set(bs_index) != periods or set(index) != {(c, p, b) for c, p in periods for b in ("monthly", "fiscal_ytd")}:
        raise ValidationError("Statement period coverage mismatch")
    detail_index = {(r["company_id"], r["period_end"], r["account_id"]): r for r in details}
    if len(detail_index) != len(details) or set(detail_index) != {(r["company_id"], r["period_end"], r["account_id"]) for r in trial}:
        raise ValidationError("Statement account coverage mismatch")
    with localcontext() as ctx:
        ctx.prec = 50
        for company, period in sorted(periods):
            day = date.fromisoformat(period)
            start = fiscal_start(day, end_months[company]).isoformat()
            month_start = day.replace(day=1).isoformat()
            totals = {b: defaultdict(Decimal) for b in ("monthly", "fiscal_ytd", "closing")}
            per_account = defaultdict(lambda: defaultdict(Decimal))
            equity_movement = ZERO
            for line in ledger:
                if line["company_id"] != company or line["entry_date"] > period:
                    continue
                spec = specs[line["account_id"]]
                value = signed(Decimal(line["debit"]) - Decimal(line["credit"]), spec["account_type"])
                for basis, lower in (("closing", "0001-01-01"), ("monthly", month_start), ("fiscal_ytd", start)):
                    if line["entry_date"] >= lower:
                        totals[basis][spec["section"]] += value
                        per_account[line["account_id"]][basis] += value
                if spec["account_type"] == "equity" and line["entry_date"] >= month_start:
                    equity_movement += value
            for basis in ("monthly", "fiscal_ytd"):
                row = index[company, period, basis]
                if row["period_start"] != (month_start if basis == "monthly" else start) or row["fiscal_year_start"] != start:
                    raise ValidationError("Income statement fiscal period metadata mismatch")
                values = totals[basis]
                for component in INCOME_COMPONENTS:
                    check(company, period, f"{basis}.{component}.ledger", Decimal(row[component]), values[component])
                check(company, period, f"{basis}.gross_profit", Decimal(row["gross_profit"]), Decimal(row["operating_revenue"]) - Decimal(row["cost_of_goods_sold"]))
                check(company, period, f"{basis}.operating_profit", Decimal(row["operating_profit"]), Decimal(row["gross_profit"]) - Decimal(row["operating_expense"]))
                check(company, period, f"{basis}.net_income", Decimal(row["net_income"]), net(values))
            bs = bs_index[company, period]
            if bs["fiscal_year_start"] != start:
                raise ValidationError("Balance sheet fiscal period metadata mismatch")
            for component in BS_COMPONENTS:
                check(company, period, f"balance_sheet.{component}.ledger", Decimal(bs[component]), totals["closing"][component])
            check(company, period, "total_assets", Decimal(bs["total_assets"]), Decimal(bs["current_assets"]) + Decimal(bs["noncurrent_assets"]))
            check(company, period, "total_liabilities", Decimal(bs["total_liabilities"]), Decimal(bs["current_liabilities"]) + Decimal(bs["noncurrent_liabilities"]))
            check(company, period, "current_profit_to_income_statement", Decimal(bs["current_fiscal_year_profit"]), Decimal(index[company, period, "fiscal_ytd"]["net_income"]))
            check(company, period, "prior_unclosed_profit", Decimal(bs["prior_unclosed_earnings"]), net(totals["closing"]) - net(totals["fiscal_ytd"]))
            check(company, period, "total_equity", Decimal(bs["total_equity"]), Decimal(bs["recorded_equity"]) + Decimal(bs["prior_unclosed_earnings"]) + Decimal(bs["current_fiscal_year_profit"]))
            check(company, period, "liabilities_and_equity_total", Decimal(bs["total_liabilities_and_equity"]), Decimal(bs["total_liabilities"]) + Decimal(bs["total_equity"]))
            check(company, period, "assets_equal_liabilities_and_equity", Decimal(bs["total_assets"]), Decimal(bs["total_liabilities_and_equity"]))
            previous = [p for c, p in periods if c == company and p < period]
            prior_equity = Decimal(bs_index[company, max(previous)]["total_equity"]) if previous else ZERO
            check(company, period, "equity_rollforward", Decimal(bs["total_equity"]) - prior_equity, equity_movement + Decimal(index[company, period, "monthly"]["net_income"]))
            for acc, spec in specs.items():
                if spec["company_id"] != company:
                    continue
                row = detail_index[company, period, acc]
                if row["section"] != spec["section"] or row["account_type"] != spec["account_type"]:
                    raise ValidationError(f"Account mapping metadata mismatch: {acc}")
                for field, basis in (("monthly_amount", "monthly"), ("fiscal_ytd_amount", "fiscal_ytd"), ("closing_amount", "closing")):
                    check(company, period, f"account.{acc}.{field}", Decimal(row[field]), per_account[acc][basis])
    failures = [c for c in checks if not c["passed"]]
    write_json(output / "statement_validation_checks.json", checks)
    write_json(output / "statement_validation_summary.json", {
        "status": "failed" if failures else "passed", "company_period_count": len(periods),
        "check_count": len(checks), "failed_check_count": len(failures), "tolerance": "0.00",
        "scope": "internal arithmetic, mapping, ledger tie-out and cross-statement consistency only; source reconciliation remains a separate gate",
        "publication_status": "draft; not certified or published"})
    if failures:
        raise ValidationError(f"{len(failures)} statement cross-check failures; see {output / 'statement_validation_summary.json'}")
    return str(output / "statement_validation_summary.json")

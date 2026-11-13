"""Posted ledger, monthly balances and strict cumulative source reconciliation.

Balances are debit-positive. No synthetic openings or year-end closings are added.
"""
import calendar
import csv
import hashlib
import json
import tempfile
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from accounting.pipeline import ValidationError, load, money, validate, write_json

ZERO = Decimal(0)
LEDGER_FIELDS = ["company_id", "account_id", "account_code", "account_name", "entry_date",
                 "journal_entry_id", "entry_number", "line_id", "description", "debit", "credit", "running_balance"]
TB_FIELDS = ["company_id", "period_end", "account_id", "account_code", "account_name",
             "opening_balance", "period_debit", "period_credit", "cumulative_debit", "cumulative_credit",
             "closing_balance", "debit_balance", "credit_balance"]


def amount(value):
    return format(value, ".2f")


def write_csv(path, fields, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def fingerprint(snapshot):
    return hashlib.sha256((Path(snapshot) / "manifest.json").read_bytes()).hexdigest()


def receipt(snapshot, output, stage, artifact, **extra):
    write_json(Path(output) / f"{stage}_summary.json", {
        "snapshot_manifest_sha256": fingerprint(snapshot), "artifact": artifact,
        "sha256": hashlib.sha256((Path(output) / artifact).read_bytes()).hexdigest(), **extra})


def read_artifact(snapshot, output, stage):
    summary = json.loads((Path(output) / f"{stage}_summary.json").read_text())
    if summary["snapshot_manifest_sha256"] != fingerprint(snapshot):
        raise ValueError(f"Stale {stage}: snapshot changed")
    path = Path(output) / summary["artifact"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != summary["sha256"]:
        raise ValueError(f"Checksum mismatch: {path}")
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def require(columns, table, fields):
    missing = set(fields) - set(columns[table])
    if missing:
        raise ValidationError(f"Missing columns in {table}: {sorted(missing)}")


def build_general_ledger(snapshot, output):
    # Protect direct Python callers as well as the CLI/DAG dependency chain.
    validate(snapshot, output)
    tables, columns = load(snapshot)
    require(columns, "journal_entries", ["posted", "entry_number"])
    require(columns, "chart_of_accounts", ["code", "name"])
    entries = {r["id"]: r for r in tables["journal_entries"]}
    accounts = {r["id"]: r for r in tables["chart_of_accounts"]}
    for entry in entries.values():
        if entry["posted"] not in {"True", "False"}:
            raise ValidationError(f"Invalid posted flag: {entry['id']}")
        if not entry["entry_number"].isdigit():
            raise ValidationError(f"Invalid entry_number: {entry['id']}")
    rows = []
    for line in tables["journal_entry_lines"]:
        entry = entries[line["journal_entry_id"]]
        if entry["posted"] != "True":
            continue
        account = accounts[line["account_id"]]
        rows.append({"company_id": entry["company_id"], "account_id": account["id"],
                     "account_code": account["code"], "account_name": account["name"],
                     "entry_date": date.fromisoformat(entry["entry_date"]).isoformat(), "journal_entry_id": entry["id"],
                     "entry_number": entry["entry_number"], "line_id": line["id"],
                     "description": line.get("description", ""),
                     "debit": amount(money(line["debit"])), "credit": amount(money(line["credit"]))})
    rows.sort(key=lambda r: (r["company_id"], r["account_id"], r["entry_date"],
                             int(r["entry_number"]), r["journal_entry_id"], r["line_id"]))
    balances = defaultdict(Decimal)
    with localcontext() as ctx:
        ctx.prec = 50
        for row in rows:
            key = row["company_id"], row["account_id"]
            balances[key] += Decimal(row["debit"]) - Decimal(row["credit"])
            row["running_balance"] = amount(balances[key])
    write_csv(Path(output) / "general_ledger.csv", LEDGER_FIELDS, rows)
    receipt(snapshot, output, "ledger", "general_ledger.csv", status="passed", line_count=len(rows),
            posted_entry_count=sum(e["posted"] == "True" for e in entries.values()),
            excluded_unposted_entry_count=sum(e["posted"] == "False" for e in entries.values()),
            opening_policy="zero before first source entry; source opening journals included; no generated closings")
    return str(Path(output) / "general_ledger.csv")


def month_end(value):
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def build_trial_balance(snapshot, output):
    tables, _ = load(snapshot)
    ledger = read_artifact(snapshot, output, "ledger")
    bounds = defaultdict(list)
    for entry in tables["journal_entries"]:
        bounds[entry["company_id"]].append(date.fromisoformat(entry["entry_date"]))
    scope = json.loads((Path(snapshot) / 'manifest.json').read_text()).get('scope')
    if scope:
        for dates in bounds.values():
            dates.append(date.fromisoformat(scope['as_of']))
    movements = defaultdict(lambda: [ZERO, ZERO])
    with localcontext() as ctx:
        ctx.prec = 50
        for row in ledger:
            key = row["company_id"], row["account_id"], row["entry_date"][:7]
            movements[key][0] += Decimal(row["debit"])
            movements[key][1] += Decimal(row["credit"])
        rows, totals = [], []
        for company, dates in sorted(bounds.items()):
            start, end = month_end(min(dates)), month_end(max(dates))
            cumulative = defaultdict(lambda: [ZERO, ZERO])
            accounts = sorted((a for a in tables["chart_of_accounts"] if a["company_id"] == company), key=lambda a: (a["code"], a["id"]))
            period = start
            while period <= end:
                total_d, total_c, closing_d, closing_c = ZERO, ZERO, ZERO, ZERO
                for account in accounts:
                    acc = account["id"]
                    cd, cc = cumulative[acc]
                    opening = cd - cc
                    d, c = movements[company, acc, period.isoformat()[:7]]
                    cd, cc = cd + d, cc + c
                    cumulative[acc] = [cd, cc]
                    closing = cd - cc
                    db, cb = max(closing, ZERO), max(-closing, ZERO)
                    rows.append(dict(zip(TB_FIELDS, [company, period.isoformat(), acc, account["code"], account["name"],
                                                      *map(amount, [opening, d, c, cd, cc, closing, db, cb])])))
                    total_d += d
                    total_c += c
                    closing_d += db
                    closing_c += cb
                totals.append({"company_id": company, "period_end": period.isoformat(),
                               "period_debit": amount(total_d), "period_credit": amount(total_c),
                               "debit_balance": amount(closing_d), "credit_balance": amount(closing_c)})
                if total_d != total_c or closing_d != closing_c:
                    raise ValidationError(f"Trial balance does not balance: {company} {period}")
                period = month_end(date(period.year + (period.month == 12), period.month % 12 + 1, 1))
    write_csv(Path(output) / "monthly_trial_balance.csv", TB_FIELDS, rows)
    receipt(snapshot, output, "trial_balance", "monthly_trial_balance.csv", status="passed", row_count=len(rows),
            company_period_count=len(totals), periods=totals,
            balance_sign="debit-positive; debit_balance/credit_balance are net closing balances")
    return str(Path(output) / "monthly_trial_balance.csv")


def reconcile_trial_balance(snapshot, output):
    return _reconcile_trial_balance(snapshot, output)


def _reconcile_trial_balance(snapshot, output, source_rows=None, report_prefix=""):
    tables, columns = load(snapshot)
    calculated = read_artifact(snapshot, output, "trial_balance")
    ledger = read_artifact(snapshot, output, "ledger")
    require(columns, "trial_balances", ["company_id", "account_id", "period_end", "debit_balance", "credit_balance"])
    key_fields = ("company_id", "period_end", "account_id")
    def key(row):
        return tuple(row[f] for f in key_fields)
    calc = {key(r): r for r in calculated}
    source, errors = {}, []
    accounts = {a["id"]: a for a in tables["chart_of_accounts"]}
    source_rows = tables["trial_balances"] if source_rows is None else source_rows
    for row in source_rows:
        k = key(row)
        try:
            period = date.fromisoformat(row["period_end"])
            if period != month_end(period) or row["period_end"] != period.isoformat():
                raise ValueError("period_end must be an ISO YYYY-MM-DD month-end")
            a = accounts.get(row["account_id"])
            if not a or a["company_id"] != row["company_id"]:
                raise ValueError("unknown account or company mismatch")
            if k in source:
                raise ValueError("duplicate company/period/account key")
            source[k] = (money(row["debit_balance"]), money(row["credit_balance"]))
        except ValueError as exc:
            errors.append({"source_id": row.get("id"), "key": list(k), "reason": str(exc)})
    results = []
    with localcontext() as ctx:
        ctx.prec = 50
        for k in sorted(set(calc) | set(source)):
            r, s = calc.get(k), source.get(k)
            cd, cc = (Decimal(r["cumulative_debit"]), Decimal(r["cumulative_credit"])) if r else (ZERO, ZERO)
            sd, sc = s if s else (ZERO, ZERO)
            if r is None:
                status = "missing_calculated"
            elif s is None:
                status = "omitted_zero" if cd == cc == ZERO else "missing_source"
            else:
                status = "matched" if (cd, cc) == (sd, sc) else "amount_mismatch"
            result = dict(zip(key_fields, k))
            result.update(status=status, calculated_cumulative_debit=amount(cd), calculated_cumulative_credit=amount(cc),
                          source_debit=amount(sd) if s else None, source_credit=amount(sc) if s else None,
                          debit_difference=amount(cd - sd), credit_difference=amount(cc - sc),
                          net_difference=amount((cd - cc) - (sd - sc)))
            # Evidence, never an automatic correction: can excluding this month's
            # month-end lines explain BOTH gross-side differences exactly?
            if status in {"amount_mismatch", "missing_source"}:
                candidates = [l for l in ledger if l["company_id"] == k[0] and l["account_id"] == k[2] and l["entry_date"] == k[1]]
                md = sum((Decimal(l["debit"]) for l in candidates), ZERO)
                mc = sum((Decimal(l["credit"]) for l in candidates), ZERO)
                explains = (md != ZERO or mc != ZERO) and (cd - sd, cc - sc) == (md, mc)
                result["month_end_lines_explain_difference"] = explains
                result["candidate_line_ids"] = [l["line_id"] for l in candidates] if explains else []
                result["candidate_descriptions"] = sorted(set(l["description"] for l in candidates)) if explains else []
            results.append(result)
    failures = [r for r in results if r["status"] not in {"matched", "omitted_zero"}]
    counts = {s: sum(r["status"] == s for r in results) for s in ("matched", "omitted_zero", "amount_mismatch", "missing_source", "missing_calculated")}
    write_json(Path(output) / f"{report_prefix}reconciliation_details.json", results)
    write_json(Path(output) / f"{report_prefix}reconciliation_source_errors.json", errors)
    report = {"status": "failed" if failures or errors else "passed", "source_row_count": len(source_rows),
              "calculated_row_count": len(calculated), "counts": counts, "difference_count": len(failures),
              "source_error_count": len(errors), "month_end_explained_count": sum(r.get("month_end_lines_explain_difference", False) for r in failures),
              "comparison_basis": "source debit_balance/credit_balance interpreted as cumulative gross debits/credits since first source entry; not monthly movement or net closing balance",
              "difference_sign": "calculated minus source", "tolerance": "0.00",
              "missing_source_policy": "only zero cumulative debit AND credit may be omitted",
              "opening_policy": "zero before first source entry; no inferred opening balances or synthetic closing entries"}
    write_json(Path(output) / f"{report_prefix}reconciliation_summary.json", report)
    if failures or errors:
        raise ValidationError(f"Reconciliation failed: {len(failures)} differences, {len(errors)} source errors; see {Path(output) / (report_prefix + 'reconciliation_summary.json')}")
    return str(Path(output) / f"{report_prefix}reconciliation_summary.json")

"""Collection, descriptive profiling and exact double-entry validation."""
import csv
import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from urllib.request import urlopen

DATASET = "mindweave/accounting-ledger-us"
REVISION = "b2f2d0890353438b4473f2c3b2d2b5e15a5132a0"
TABLES = ("companies", "chart_of_accounts", "journal_entries", "journal_entry_lines", "trial_balances")
REQUIRED = {
    "companies": {"id"},
    "chart_of_accounts": {"id", "company_id", "account_type"},
    "journal_entries": {"id", "company_id", "entry_date"},
    "journal_entry_lines": {"id", "journal_entry_id", "account_id", "debit", "credit"},
    "trial_balances": set(),
}


class ValidationError(ValueError):
    pass


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temporary.replace(path)


def collect(root, revision=REVISION):
    """Only publish a manifest after all five immutable source files exist."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full 40-character commit SHA")
    snapshot = Path(root).resolve() / "raw" / revision
    snapshot.mkdir(parents=True, exist_ok=True)
    manifest = {"dataset": DATASET, "revision": revision, "license": "cc-by-nc-4.0", "files": {}}
    for table in TABLES:
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{revision}/data/{table}.csv"
        target = snapshot / f"{table}.csv"
        # A complete manifest makes cached snapshots reusable and detects local corruption.
        if (snapshot / "manifest.json").exists():
            old = json.loads((snapshot / "manifest.json").read_text())
            expected = old["files"][table]["sha256"]
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Snapshot checksum mismatch: {table}")
        else:
            with urlopen(url, timeout=120) as response:
                payload = response.read()
            with tempfile.NamedTemporaryFile(dir=snapshot, delete=False) as f:
                temporary = Path(f.name)
                f.write(payload)
            temporary.replace(target)
        payload = target.read_bytes()
        manifest["files"][table] = {"url": url, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    write_json(snapshot / "manifest.json", manifest)
    return str(snapshot)


def load(snapshot):
    snapshot = Path(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    tables = {}
    columns = {}
    for table in TABLES:
        path = snapshot / f"{table}.csv"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][table]["sha256"]:
            raise ValueError(f"Snapshot checksum mismatch: {table}")
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            if len(set(fields)) != len(fields) or not fields:
                raise ValueError(f"Invalid CSV header: {table}")
            if REQUIRED[table] - set(fields):
                raise ValueError(f"Missing columns in {table}: {sorted(REQUIRED[table] - set(fields))}")
            rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in rows):
                raise ValueError(f"Malformed CSV row: {table}")
            tables[table], columns[table] = rows, fields
    if 'scope' in manifest:
        from accounting.periods import verify_scope
        verify_scope(snapshot, manifest, tables, columns)
    return tables, columns


def profile(snapshot, output):
    tables, columns = load(snapshot)
    report = {"snapshot": str(snapshot), "tables": {}}
    for table, rows in tables.items():
        details = {}
        for field in columns[table]:
            values = [row[field] for row in rows]
            nonempty = [v for v in values if v.strip()]
            item = {"null_or_blank_count": len(values) - len(nonempty), "distinct_count": len(set(nonempty))}
            if field in {"status", "posted", "is_adjusting", "is_reversal", "source_module", "account_type", "currency", "currency_code"}:
                item["value_counts"] = dict(Counter(nonempty))
            if field in {"debit", "credit", "tax_amount", "debit_balance", "credit_balance"}:
                amounts = []
                for value in nonempty:
                    try:
                        amounts.append(money(value))
                    except ValueError:
                        pass
                with localcontext() as ctx:
                    ctx.prec = 50
                    item.update(valid_amount_count=len(amounts), invalid_amount_count=len(values) - len(amounts),
                                min=str(min(amounts)) if amounts else None,
                                max=str(max(amounts)) if amounts else None, total=str(sum(amounts, Decimal(0))))
            if field.endswith("date") or field in {"period_start", "period_end"}:
                valid = []
                for value in nonempty:
                    try:
                        valid.append(date.fromisoformat(value))
                    except ValueError:
                        pass
                item.update(valid_date_count=len(valid), invalid_date_count=len(nonempty) - len(valid),
                            min=str(min(valid)) if valid else None, max=str(max(valid)) if valid else None)
            details[field] = item
        ids = [r["id"] for r in rows] if "id" in columns[table] else []
        report["tables"][table] = {"row_count": len(rows), "duplicate_id_count": len(ids) - len(set(ids)), "columns": details}
    path = Path(output) / "profile.json"
    write_json(path, report)
    return str(path)


def money(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("not a decimal amount")
    if not number.is_finite() or number < 0 or number > Decimal("1e20"):
        raise ValueError("amount must be finite, nonnegative and <= 1e20")
    if number.as_tuple().exponent < -2:
        raise ValueError("amount has more than two decimal places")
    return number


def validate(snapshot, output):
    tables, _ = load(snapshot)
    errors = []
    def error(code, table, record_id, **extra):
        errors.append(dict(code=code, table=table, record_id=record_id, **extra))
    for table, rows in tables.items():
        if not rows:
            error("empty_table", table, None)
        seen = set()
        for row in rows:
            if "id" not in row:
                continue
            if not row["id"].strip():
                error("missing_id", table, row["id"])
            elif row["id"] in seen:
                error("duplicate_id", table, row["id"])
            seen.add(row["id"])
    companies = {r["id"] for r in tables["companies"]}
    accounts = {r["id"]: r for r in tables["chart_of_accounts"]}
    entries = {r["id"]: r for r in tables["journal_entries"]}
    for table in ("chart_of_accounts", "journal_entries"):
        for row in tables[table]:
            if row["company_id"] not in companies:
                error("unknown_company", table, row["id"])
            if table == "journal_entries":
                try:
                    date.fromisoformat(row["entry_date"])
                except ValueError:
                    error("invalid_entry_date", table, row["id"])
    groups = defaultdict(list)
    for row in tables["journal_entry_lines"]:
        entry = entries.get(row["journal_entry_id"])
        account = accounts.get(row["account_id"])
        if entry is None:
            error("unknown_journal_entry", "journal_entry_lines", row["id"])
        if account is None:
            error("unknown_account", "journal_entry_lines", row["id"])
        if entry and account and entry["company_id"] != account["company_id"]:
            error("company_mismatch", "journal_entry_lines", row["id"])
        groups[row["journal_entry_id"]].append(row)
    results = []
    with localcontext() as ctx:
        ctx.prec = 50
        for entry_id in entries:
            lines = groups[entry_id]
            debit, credit = Decimal(0), Decimal(0)
            invalid = False
            if len(lines) < 2:
                error("insufficient_lines", "journal_entries", entry_id)
                invalid = True
            for line in lines:
                try:
                    d, c = money(line["debit"]), money(line["credit"])
                except ValueError as exc:
                    error("invalid_amount", "journal_entry_lines", line["id"], reason=str(exc))
                    invalid = True
                    continue
                if (d == 0 and c == 0) or (d > 0 and c > 0):
                    error("invalid_debit_credit_side", "journal_entry_lines", line["id"])
                    invalid = True
                debit += d
                credit += c
            if debit != credit:
                error("unbalanced_entry", "journal_entries", entry_id, difference=str(debit - credit))
            results.append({"journal_entry_id": entry_id, "line_count": len(lines), "debit": str(debit),
                            "credit": str(credit), "difference": str(debit - credit), "balanced": not invalid and debit == credit})
    report = {"snapshot": str(snapshot), "status": "failed" if errors else "passed",
              "entry_count": len(entries), "line_count": len(tables["journal_entry_lines"]),
              "balanced_entry_count": sum(r["balanced"] for r in results),
              "error_count": len(errors), "error_counts": dict(Counter(e["code"] for e in errors))}
    output = Path(output)
    write_json(output / "journal_balances.json", results)
    write_json(output / "validation_errors.json", errors)
    write_json(output / "validation_summary.json", report)
    if errors:
        raise ValidationError(f"{len(errors)} validation errors; see {output / 'validation_summary.json'}")
    return str(output / "validation_summary.json")

"""Direct-method cash flows, with explicit account policy and source tie-outs."""
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from accounting.ledger import ZERO, amount, read_artifact, receipt, write_csv
from accounting.pipeline import ValidationError, load, write_json
from accounting.statements import configuration, fiscal_start

ACTIVITIES = ('operating', 'investing', 'financing')
FIELDS = ['company_id', 'period_start', 'period_end', 'basis', 'beginning_cash'] + [
    f'{a}_{v}' for a in ACTIVITIES for v in ('inflows', 'outflows', 'net')] + ['net_change', 'ending_cash']
DETAIL_FIELDS = ['company_id', 'entry_date', 'journal_entry_id', 'activity', 'inflow', 'outflow', 'net_cash', 'cash_line_ids', 'counterpart_line_ids']
EXCLUSION_FIELDS = ['company_id', 'entry_date', 'journal_entry_id', 'reason', 'line_ids']
ARTIFACTS = ('cash_flow_statement.csv', 'cash_flow_summary.json', 'cash_flow_details.csv', 'cash_flow_details_summary.json',
             'cash_flow_exclusions.csv', 'cash_flow_exclusions_summary.json', 'cash_flow_validation_checks.json', 'cash_flow_validation_summary.json')


def policy(tables, columns, mapping):
    _, months, specs = configuration(tables, columns, mapping)
    cash = set()
    for account in tables['chart_of_accounts']:
        spec = specs[account['id']]
        role = spec.get('cash_flow_activity')
        if role not in {'cash', *ACTIVITIES}:
            raise ValidationError(f"Missing/invalid cash flow classification: {account['code']}")
        if role == 'cash':
            if account['account_type'] != 'asset' or account['sub_type'] != 'current_asset':
                raise ValidationError(f"Cash account must be a current asset: {account['code']}")
            cash.add(account['id'])
    for company in tables['companies']:
        if not any(a['company_id'] == company['id'] and a['id'] in cash for a in tables['chart_of_accounts']):
            raise ValidationError(f"No cash accounts mapped for company: {company['id']}")
    return months, specs, cash


def classify(lines, specs, cash):
    """One activity can be netted (e.g. payroll); mixed activities need one-sided counterparts."""
    cash_lines = [l for l in lines if l['account_id'] in cash]
    other = [l for l in lines if l['account_id'] not in cash]
    if not cash_lines:
        return {}, 'noncash', cash_lines, other
    net_cash = sum((Decimal(l['debit']) - Decimal(l['credit']) for l in cash_lines), ZERO)
    if not other:
        if net_cash != ZERO:
            raise ValidationError('Unbalanced internal cash transfer')
        return {}, 'internal_transfer', cash_lines, other
    if any(Decimal(l['debit']) > ZERO for l in cash_lines) and any(Decimal(l['credit']) > ZERO for l in cash_lines):
        raise ValidationError('Ambiguous cash transfer with external legs; explicit allocation required')
    roles = {specs[l['account_id']]['cash_flow_activity'] for l in other}
    if len(roles) == 1:
        return {next(iter(roles)): net_cash}, None, cash_lines, other
    if any(Decimal(l['debit']) > ZERO for l in other) and any(Decimal(l['credit']) > ZERO for l in other):
        raise ValidationError('Ambiguous mixed-activity journal; explicit allocation required')
    values = defaultdict(Decimal)
    for line in other:
        values[specs[line['account_id']]['cash_flow_activity']] += Decimal(line['credit']) - Decimal(line['debit'])
    if sum(values.values(), ZERO) != net_cash:
        raise ValidationError('Cash flow allocation does not equal cash movement')
    return dict(values), None, cash_lines, other


def build_cash_flow(snapshot, output):
    output = Path(output)
    write_json(output / 'cash_flow_summary.json', {'status': 'building'})
    try:
        tables, columns = load(snapshot)
        months, specs, cash = policy(tables, columns, output / 'statement_mapping.json')
        ledger = read_artifact(snapshot, output, 'ledger')
        trial = read_artifact(snapshot, output, 'trial_balance')
        groups = defaultdict(list)
        for line in ledger:
            groups[line['journal_entry_id']].append(line)
        details, exclusions, rows = [], [], []
        with localcontext() as ctx:
            ctx.prec = 50
            for ident, lines in sorted(groups.items()):
                values, reason, cash_lines, other = classify(lines, specs, cash)
                base = {'company_id': lines[0]['company_id'], 'entry_date': lines[0]['entry_date'], 'journal_entry_id': ident}
                if reason:
                    exclusions.append({**base, 'reason': reason, 'line_ids': json.dumps(sorted(l['line_id'] for l in lines))})
                for activity, value in sorted(values.items()):
                    details.append({**base, 'activity': activity, 'inflow': amount(max(value, ZERO)),
                        'outflow': amount(max(-value, ZERO)), 'net_cash': amount(value),
                        'cash_line_ids': json.dumps(sorted(l['line_id'] for l in cash_lines)),
                        'counterpart_line_ids': json.dumps(sorted(l['line_id'] for l in other if specs[l['account_id']]['cash_flow_activity'] == activity))})
            for company, period in sorted({(r['company_id'], r['period_end']) for r in trial}):
                day = date.fromisoformat(period)
                for basis, start in [('monthly', day.replace(day=1).isoformat()), ('fiscal_ytd', fiscal_start(day, months[company]).isoformat())]:
                    opening = sum((Decimal(l['debit']) - Decimal(l['credit']) for l in ledger if l['company_id'] == company and l['account_id'] in cash and l['entry_date'] < start), ZERO)
                    values = defaultdict(Decimal)
                    for detail in details:
                        if detail['company_id'] == company and start <= detail['entry_date'] <= period:
                            values[detail['activity'] + '_inflows'] += Decimal(detail['inflow'])
                            values[detail['activity'] + '_outflows'] += Decimal(detail['outflow'])
                    for activity in ACTIVITIES:
                        values[activity + '_net'] = values[activity + '_inflows'] - values[activity + '_outflows']
                    change = sum((values[a + '_net'] for a in ACTIVITIES), ZERO)
                    rows.append({'company_id': company, 'period_start': start, 'period_end': period, 'basis': basis,
                        'beginning_cash': amount(opening), **{k: amount(v) for k, v in values.items()},
                        'net_change': amount(change), 'ending_cash': amount(opening + change)})
        for stage, filename, fields, data in [('cash_flow', 'cash_flow_statement.csv', FIELDS, rows),
                ('cash_flow_details', 'cash_flow_details.csv', DETAIL_FIELDS, details),
                ('cash_flow_exclusions', 'cash_flow_exclusions.csv', EXCLUSION_FIELDS, exclusions)]:
            write_csv(output / filename, fields, data)
            receipt(snapshot, output, stage, filename, status='draft', row_count=len(data),
                method='direct', policy='explicit account roles; internal cash transfers and noncash journals excluded',
                scope='source book currency, zero before first source entry; no inferred FX effects or missing opening balances')
    except Exception as exc:
        write_json(output / 'cash_flow_summary.json', {'status': 'failed', 'error': str(exc)})
        raise
    return str(output / 'cash_flow_statement.csv')


def validate_cash_flow(snapshot, output):
    output = Path(output)
    write_json(output / 'cash_flow_validation_summary.json', {'status': 'running'})
    try:
        return _validate(snapshot, output)
    except Exception as exc:
        state = json.loads((output / 'cash_flow_validation_summary.json').read_text())
        if state['status'] == 'running':
            write_json(output / 'cash_flow_validation_summary.json', {'status': 'failed', 'error': str(exc)})
        raise


def _validate(snapshot, output):
    tables, columns = load(snapshot)
    months, specs, cash = policy(tables, columns, output / 'statement_mapping.json')
    rows = read_artifact(snapshot, output, 'cash_flow')
    details = read_artifact(snapshot, output, 'cash_flow_details')
    exclusions = read_artifact(snapshot, output, 'cash_flow_exclusions')
    trial = read_artifact(snapshot, output, 'trial_balance')
    balance = read_artifact(snapshot, output, 'balance_sheet')
    account_rows = read_artifact(snapshot, output, 'statement_accounts')
    periods = {(r['company_id'], r['period_end']) for r in trial}
    keys = [(r['company_id'], r['period_end'], r['basis']) for r in rows]
    if len(set(keys)) != len(keys) or set(keys) != {(c, p, b) for c, p in periods for b in ('monthly', 'fiscal_ytd')}:
        raise ValidationError('Cash flow period coverage mismatch')
    entries = {e['id']: e for e in tables['journal_entries'] if e['posted'] == 'True'}
    raw = defaultdict(list)
    for line in tables['journal_entry_lines']:
        if line['journal_entry_id'] in entries:
            raw[line['journal_entry_id']].append(line)
    checks = []
    def check(company, period, label, actual, expected):
        checks.append({'company_id': company, 'period_end': period, 'check': label, 'actual': amount(actual),
                       'expected': amount(expected), 'difference': amount(actual - expected), 'passed': actual == expected})
    expected_details, expected_exclusions = {}, {}
    with localcontext() as ctx:
        ctx.prec = 50
        # Independently classify source journals (not generated ledger/detail sums).
        for ident, lines in raw.items():
            values, reason, cash_lines, other = classify(lines, specs, cash)
            if reason:
                expected_exclusions[ident] = (reason, sorted(l['id'] for l in lines))
            for activity, value in values.items():
                expected_details[ident, activity] = (value, sorted(l['id'] for l in cash_lines),
                    sorted(l['id'] for l in other if specs[l['account_id']]['cash_flow_activity'] == activity))
        detail_keys = [(r['journal_entry_id'], r['activity']) for r in details]
        if len(set(detail_keys)) != len(detail_keys) or set(detail_keys) != set(expected_details):
            raise ValidationError('Cash flow source-journal coverage mismatch')
        exclusion_keys = [r['journal_entry_id'] for r in exclusions]
        if len(set(exclusion_keys)) != len(exclusion_keys) or set(exclusion_keys) != set(expected_exclusions):
            raise ValidationError('Cash flow exclusion coverage mismatch')
        for r in exclusions:
            e = entries[r['journal_entry_id']]
            if (r['reason'], json.loads(r['line_ids'])) != expected_exclusions[r['journal_entry_id']] or r['company_id'] != e['company_id'] or r['entry_date'] != e['entry_date']:
                raise ValidationError('Incorrect noncash/internal-transfer exclusion')
        for r in details:
            e = entries[r['journal_entry_id']]
            expected, cash_ids, other_ids = expected_details[r['journal_entry_id'], r['activity']]
            if (r['company_id'], r['entry_date'], json.loads(r['cash_line_ids']), json.loads(r['counterpart_line_ids'])) != (e['company_id'], e['entry_date'], cash_ids, other_ids):
                raise ValidationError('Cash flow evidence mismatch')
            for key, value in [('inflow', max(expected, ZERO)), ('outflow', max(-expected, ZERO)), ('net_cash', expected)]:
                check(e['company_id'], e['entry_date'], f"journal.{e['id']}.{r['activity']}.{key}", Decimal(r[key]), value)
        for row in rows:
            company, period, basis = row['company_id'], row['period_end'], row['basis']
            day = date.fromisoformat(period)
            start = day.replace(day=1).isoformat() if basis == 'monthly' else fiscal_start(day, months[company]).isoformat()
            if row['period_start'] != start:
                raise ValidationError('Cash flow fiscal period metadata mismatch')
            opening, closing = ZERO, ZERO
            for ident, lines in raw.items():
                e = entries[ident]
                if e['company_id'] == company:
                    value = sum((Decimal(l['debit']) - Decimal(l['credit']) for l in lines if l['account_id'] in cash), ZERO)
                    if e['entry_date'] < start:
                        opening += value
                    if e['entry_date'] <= period:
                        closing += value
            for activity in ACTIVITIES:
                values = [v[0] for (ident, a), v in expected_details.items() if a == activity and entries[ident]['company_id'] == company and start <= entries[ident]['entry_date'] <= period]
                inflow = sum((max(v, ZERO) for v in values), ZERO)
                outflow = sum((max(-v, ZERO) for v in values), ZERO)
                for suffix, expected in [('inflows', inflow), ('outflows', outflow), ('net', inflow - outflow)]:
                    check(company, period, basis + '.' + activity + '_' + suffix, Decimal(row[activity + '_' + suffix]), expected)
            check(company, period, basis + '.beginning_cash.source', Decimal(row['beginning_cash']), opening)
            check(company, period, basis + '.ending_cash.source', Decimal(row['ending_cash']), closing)
            check(company, period, basis + '.net_change', Decimal(row['net_change']), sum((Decimal(row[a + '_net']) for a in ACTIVITIES), ZERO))
            check(company, period, basis + '.cash_rollforward', Decimal(row['beginning_cash']) + Decimal(row['net_change']), Decimal(row['ending_cash']))
            tb_cash = sum((Decimal(r['closing_balance']) for r in trial if r['company_id'] == company and r['period_end'] == period and r['account_id'] in cash), ZERO)
            bs_cash = sum((Decimal(r['closing_amount']) for r in account_rows if r['company_id'] == company and r['period_end'] == period and r['account_id'] in cash), ZERO)
            check(company, period, basis + '.trial_balance_cash', Decimal(row['ending_cash']), tb_cash)
            check(company, period, basis + '.balance_sheet_accounts_cash', Decimal(row['ending_cash']), bs_cash)
            bs_row = [r for r in balance if r['company_id'] == company and r['period_end'] == period]
            if len(bs_row) != 1:
                raise ValidationError('Balance sheet period coverage mismatch')
            check(company, period, basis + '.balance_sheet_cash', Decimal(bs_row[0]['cash_and_cash_equivalents']), closing)
    write_json(output / 'cash_flow_validation_checks.json', checks)
    failures = sum(not c['passed'] for c in checks)
    write_json(output / 'cash_flow_validation_summary.json', {'status': 'failed' if failures else 'passed',
        'check_count': len(checks), 'failed_check_count': failures, 'row_count': len(rows),
        'cash_journal_activity_count': len(details), 'excluded_journal_count': len(exclusions), 'tolerance': '0.00', 'method': 'direct'})
    if failures:
        raise ValidationError(f'{failures} cash flow validation failures')
    return str(output / 'cash_flow_validation_summary.json')

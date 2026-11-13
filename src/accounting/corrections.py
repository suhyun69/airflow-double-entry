"""Evidence-based supplemental source trial balances; raw snapshots never change."""
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from accounting.ledger import (ZERO, amount, month_end, read_artifact, receipt, reconcile_trial_balance, require,
                               _reconcile_trial_balance, write_csv)
from accounting.pipeline import ValidationError, load, write_json

POLICY = {
    "version": 1, "rule": "missing_current_month_end_depreciation_v1",
    "expense_code": "6-2800", "contra_asset_codes": ["1-2010", "1-2110", "1-2210"],
    "requirements": ["posted assets-module journal dated month-end", "Depreciation — description prefix",
        "exactly one depreciation expense debit and one accumulated-depreciation credit of equal amount",
        "complete current-month depreciation batch exactly explains both gross-side differences",
        "all batch counterpart lines are included; correction debits equal credits for every company/month",
        "no other source errors or unexplained differences"],
    "method": "supplement each source cumulative snapshot with ONLY that month's missing batch; never carry adjustments forward again",
    "limitations": "source snapshot timing is inferred; original journal depreciation amounts are used, not recalculated useful lives or tax rules"}
EXTRA_ARTIFACTS = (
    "correction_policy.json", "reconciliation_analysis.json", "reconciliation_analysis_summary.json", "reconciliation_analysis.md",
    "correction_journal_evidence.csv", "correction_log.json", "adjusted_source_trial_balances.csv",
    "adjusted_source_summary.json", "adjusted_reconciliation_details.json",
    "adjusted_reconciliation_source_errors.json", "adjusted_reconciliation_summary.json")
KEY = ("company_id", "period_end", "account_id")
SOURCE_FIELDS = ["id", "company_id", "period_end", "account_id", "account_code", "account_name", "debit_balance", "credit_balance"]
EVIDENCE_FIELDS = ["company_id", "period_end", "journal_entry_id", "entry_number", "line_id", "account_id", "account_code",
                   "description", "source_module", "source_id", "debit", "credit"]


def row_key(row):
    return tuple(row[k] for k in KEY)


def analyze_reconciliation(snapshot, output):
    """Prove a narrowly defined missing batch; unexplained differences stay blocked."""
    output = Path(output)
    write_json(output / 'reconciliation_analysis_summary.json', {'status': 'running'})
    tables, columns = load(snapshot)
    require(columns, 'trial_balances', ['company_id', 'period_end', 'account_id', 'debit_balance', 'credit_balance'])
    try:
        reconcile_trial_balance(snapshot, output)
    except ValidationError:
        pass  # Raw failure is an input to analysis, never changed to 'passed'.
    summary = json.loads((output / 'reconciliation_summary.json').read_text())
    raw = json.loads((output / 'reconciliation_details.json').read_text())
    ledger = read_artifact(snapshot, output, 'ledger')
    entries = {e['id']: e for e in tables['journal_entries']}
    accounts = {a['id']: a for a in tables['chart_of_accounts']}
    groups = defaultdict(list)
    for line in ledger:
        groups[line['journal_entry_id']].append(line)
    approved = defaultdict(list)
    for ident, lines in groups.items():
        entry = entries[ident]
        day = date.fromisoformat(entry['entry_date'])
        if (entry.get('posted') != 'True' or entry.get('source_module') != 'assets' or
                not entry.get('description', '').startswith('Depreciation — ') or day != month_end(day) or len(lines) != 2):
            continue
        expense = [l for l in lines if accounts[l['account_id']]['code'] == POLICY['expense_code']
                   and accounts[l['account_id']]['account_type'] == 'expense'
                   and accounts[l['account_id']].get('sub_type') == 'operating_expense'
                   and Decimal(l['debit']) > ZERO and Decimal(l['credit']) == ZERO]
        contra = [l for l in lines if accounts[l['account_id']]['code'] in POLICY['contra_asset_codes']
                  and accounts[l['account_id']]['account_type'] == 'asset'
                  and accounts[l['account_id']].get('sub_type') == 'fixed_asset'
                  and Decimal(l['credit']) > ZERO and Decimal(l['debit']) == ZERO]
        if len(expense) == len(contra) == 1 and Decimal(expense[0]['debit']) == Decimal(contra[0]['credit']):
            for line in lines:
                approved[line['company_id'], day.isoformat(), line['account_id']].append({
                    **{f: line[f] for f in ['company_id', 'journal_entry_id', 'entry_number', 'line_id', 'account_id', 'account_code', 'description', 'debit', 'credit']},
                    'period_end': day.isoformat(), 'source_module': entry['source_module'], 'source_id': entry.get('source_id', '')})
    differences = [r for r in raw if r['status'] not in {'matched', 'omitted_zero'}]
    analysis, evidence = [], []
    with localcontext() as ctx:
        ctx.prec = 50
        for row in differences:
            k = row_key(row)
            candidates = approved.get(k, [])
            d = sum((Decimal(l['debit']) for l in candidates), ZERO)
            c = sum((Decimal(l['credit']) for l in candidates), ZERO)
            eligible = (row['status'] in {'amount_mismatch', 'missing_source'} and bool(candidates) and
                        (Decimal(row['debit_difference']), Decimal(row['credit_difference'])) == (d, c))
            analysis.append({**row, 'account_code': accounts.get(k[2], {}).get('code'),
                'account_name': accounts.get(k[2], {}).get('name'), 'eligible': eligible,
                'rule': POLICY['rule'] if eligible else None,
                'reason': 'exact missing current-month depreciation batch' if eligible else 'not fully explained by approved depreciation rule',
                'adjustment_debit': amount(d) if eligible else '0.00', 'adjustment_credit': amount(c) if eligible else '0.00',
                'evidence_line_ids': sorted(l['line_id'] for l in candidates) if eligible else []})
            if eligible:
                evidence.extend(candidates)
        # Do not accept isolated account-side repairs; every full journal must be present.
        evidence_ids = {l['line_id'] for l in evidence}
        entry_ids = {l['journal_entry_id'] for l in evidence}
        complete = all({l['line_id'] for l in groups[e]} <= evidence_ids for e in entry_ids)
        totals = defaultdict(lambda: [ZERO, ZERO, set()])
        for line in evidence:
            k = line['company_id'], line['period_end']
            totals[k][0] += Decimal(line['debit'])
            totals[k][1] += Decimal(line['credit'])
            totals[k][2].add(line['journal_entry_id'])
        balanced = all(d == c for d, c, _ in totals.values())
        can_adjust = summary['source_error_count'] == 0 and all(r['eligible'] for r in analysis) and complete and balanced
        periods = []
        for (company, period), (d, c, ids) in sorted(totals.items()):
            nums = [int(entries[e]['entry_number']) for e in ids]
            other = [int(e['entry_number']) for e in entries.values() if e['company_id'] == company
                     and e['entry_date'][:7] == period[:7] and e['id'] not in ids and e.get('posted') == 'True']
            periods.append({'company_id': company, 'period_end': period, 'debit': amount(d), 'credit': amount(c),
                'journal_count': len(ids), 'first_depreciation_entry_number': min(nums), 'last_depreciation_entry_number': max(nums),
                'last_other_entry_number': max(other) if other else None,
                'depreciation_follows_other_entries': not other or min(nums) > max(other)})
        report = {'status': 'eligible' if can_adjust else 'blocked', 'raw_difference_count': len(differences),
            'eligible_difference_count': sum(r['eligible'] for r in analysis), 'unexplained_difference_count': sum(not r['eligible'] for r in analysis),
            'source_error_count': summary['source_error_count'], 'complete_journals': complete, 'balanced_adjustments': balanced,
            'evidence_journal_count': len(entry_ids), 'evidence_line_count': len(evidence),
            'total_adjustment_debit': amount(sum((v[0] for v in totals.values()), ZERO)),
            'total_adjustment_credit': amount(sum((v[1] for v in totals.values()), ZERO)), 'periods': periods,
            'conclusion': 'Source snapshots omit exactly current-month posted depreciation; previous months are already included. Generation timing remains an inference.' if can_adjust and differences else 'No correction required' if can_adjust else 'Unexplained differences or incomplete journal evidence block correction',
            'rule': POLICY['rule']}
        account_totals = defaultdict(lambda: [ZERO, ZERO])
        for line in evidence:
            account_totals[line['company_id'], line['account_id']][0] += Decimal(line['debit'])
            account_totals[line['company_id'], line['account_id']][1] += Decimal(line['credit'])
        report['account_totals'] = [{'company_id': company, 'account_id': acc, 'account_code': accounts[acc]['code'],
            'account_name': accounts[acc]['name'], 'debit': amount(d), 'credit': amount(c)}
            for (company, acc), (d, c) in sorted(account_totals.items())]
    write_json(output / 'correction_policy.json', POLICY)
    write_json(output / 'reconciliation_analysis.json', analysis)
    write_json(output / 'reconciliation_analysis_summary.json', report)
    write_csv(output / 'correction_journal_evidence.csv', EVIDENCE_FIELDS,
              sorted(evidence, key=lambda r: (r['company_id'], r['period_end'], int(r['entry_number']), r['line_id'])))
    markdown = ['# 원천 시산표 대사 차이 분석', '',
        f"원본 차이 {len(differences)}행 중 보정 가능 {report['eligible_difference_count']}행, 미설명 {report['unexplained_difference_count']}행.", '',
        '## 확인한 사실과 해석', '',
        '- 전기된 원천 분개를 재계산한 누적 차변·대변과 원천 시산표를 각각 비교했다.',
        '- 보정 가능 행의 차이는 해당 월말 assets 모듈 감가상각 전표 전체 배치와 양쪽 금액이 정확히 같다.',
        '- 이전 달 감가상각은 해당 월 원천 누적액에 이미 포함되어 있다. 각 월에 그 달의 누락분만 더한다.',
        '- 전표번호 순서도 월말 감가상각이 다른 거래 뒤에 생성된 패턴을 뒷받침한다. 원천 시산표의 생성 타임스탬프는 없으므로 생성 시점 차이라는 해석은 추정이다.',
        '- 자산 원가·내용연수 자료가 없어 감가상각액 자체의 적정성은 검증하지 않는다. 이미 전기된 분개 금액을 그대로 연결한다.', '',
        f"근거 전표 {len(entry_ids)}건, 라인 {len(evidence)}건. 보정 합계 차변 {report['total_adjustment_debit']}, 대변 {report['total_adjustment_credit']}.", '',
        '## 계정별 누락분 합계', '', '| 계정 | 계정명 | 추가 차변 | 추가 대변 |', '|---|---|---:|---:|']
    markdown += [f"| {r['account_code']} | {r['account_name'].replace('|', '/')} | {r['debit']} | {r['credit']} |" for r in report['account_totals']]
    markdown += ['', '## 월별 근거', '', '| 회사 | 월말 | 추가 차변 | 추가 대변 | 전표 수 | 기타 전표 마지막 번호 | 감가상각 첫 번호 |', '|---|---|---:|---:|---:|---:|---:|']
    markdown += [f"| {p['company_id']} | {p['period_end']} | {p['debit']} | {p['credit']} | {p['journal_count']} | {p['last_other_entry_number']} | {p['first_depreciation_entry_number']} |" for p in periods]
    markdown += ['', '## 차이 행 전체', '', '| 월말 | 계정 | 원천 차변 | 원천 대변 | 누락 차변 | 누락 대변 | 보정 가능 |', '|---|---|---:|---:|---:|---:|---|']
    markdown += [f"| {r['period_end']} | {r['account_code']} | {r['source_debit']} | {r['source_credit']} | {r['adjustment_debit']} | {r['adjustment_credit']} | {r['eligible']} |" for r in analysis]
    markdown += ['', '## 보존 및 차단 조건', '',
        '원천 CSV와 원본 대사 실패 결과는 수정하지 않는다. 보정본·보정 전후 금액·근거 라인 ID를 별도 파일로 남긴다.',
        '정책에 맞지 않는 거래, 일부 분개만으로 설명되는 차이, 키/참조 오류, 미설명 차이는 보정 및 최종 발행을 차단한다.', '']
    (output / 'reconciliation_analysis.md').write_text('\n'.join(markdown), encoding='utf-8')
    return str(output / 'reconciliation_analysis_summary.json')


def build_adjusted_trial_balance(snapshot, output):
    output = Path(output)
    write_json(output / 'adjusted_source_summary.json', {'status': 'building'})
    try:
        analyze_reconciliation(snapshot, output)
        summary = json.loads((output / 'reconciliation_analysis_summary.json').read_text())
        if summary['status'] != 'eligible':
            raise ValidationError('Source correction blocked: unexplained differences or incomplete depreciation evidence')
        tables, _ = load(snapshot)
        accounts = {a['id']: a for a in tables['chart_of_accounts']}
        source = {row_key(r): dict(r) for r in tables['trial_balances']}
        analysis = json.loads((output / 'reconciliation_analysis.json').read_text())
        log = []
        with localcontext() as ctx:
            ctx.prec = 50
            for row in analysis:
                k = row_key(row)
                original = source.get(k)
                account = accounts[k[2]]
                base_d = Decimal(original['debit_balance']) if original else ZERO
                base_c = Decimal(original['credit_balance']) if original else ZERO
                d, c = Decimal(row['adjustment_debit']), Decimal(row['adjustment_credit'])
                adjusted = dict(original) if original else {'id': 'supplement-' + hashlib.sha256('|'.join(k).encode()).hexdigest(),
                    **dict(zip(KEY, k)), 'account_code': account['code'], 'account_name': account['name']}
                adjusted.update(debit_balance=amount(base_d + d), credit_balance=amount(base_c + c))
                source[k] = adjusted
                log.append({**dict(zip(KEY, k)), 'original_source_id': original.get('id') if original else None,
                    'operation': 'supplement_existing' if original else 'insert_missing', 'rule': POLICY['rule'],
                    'original_debit': amount(base_d) if original else None, 'original_credit': amount(base_c) if original else None,
                    'adjustment_debit': amount(d), 'adjustment_credit': amount(c), 'adjusted_debit': adjusted['debit_balance'],
                    'adjusted_credit': adjusted['credit_balance'], 'evidence_line_ids': row['evidence_line_ids']})
        # Preserve untouched source values; no calculated-target balances are copied in.
        write_csv(output / 'adjusted_source_trial_balances.csv', SOURCE_FIELDS, [
            {f: source[k].get(f, '') for f in SOURCE_FIELDS} for k in sorted(source)])
        write_json(output / 'correction_log.json', log)
        receipt(snapshot, output, 'adjusted_source', 'adjusted_source_trial_balances.csv', status='passed',
                row_count=len(source), correction_count=len(log), inserted_row_count=sum(r['operation'] == 'insert_missing' for r in log),
                rule=POLICY['rule'], scope='supplemented source cumulative gross amounts; raw source and posted ledger unchanged')
    except Exception as exc:
        write_json(output / 'adjusted_source_summary.json', {'status': 'failed', 'error': str(exc)})
        raise
    return str(output / 'adjusted_source_trial_balances.csv')


def reconcile_adjusted_trial_balance(snapshot, output):
    output = Path(output)
    summary = json.loads((output / 'adjusted_source_summary.json').read_text())
    if summary.get('status') != 'passed':
        raise ValidationError('Adjusted source generation did not complete successfully')
    rows = read_artifact(snapshot, output, 'adjusted_source')
    return _reconcile_trial_balance(snapshot, output, rows, 'adjusted_')

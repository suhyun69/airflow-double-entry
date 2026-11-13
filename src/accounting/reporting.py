"""Offline HTML reports and a fail-closed, immutable local publication gate."""
import hashlib
import json
import shutil
import tempfile
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path

from accounting.ledger import build_general_ledger, build_trial_balance, fingerprint, read_artifact, reconcile_trial_balance
from accounting.pipeline import ValidationError, load, profile, validate, write_json
from accounting.statements import build_statements, validate_statements
from accounting.corrections import EXTRA_ARTIFACTS, build_adjusted_trial_balance, reconcile_adjusted_trial_balance
from accounting.cash_flow import ARTIFACTS as CASH_FLOW_ARTIFACTS, build_cash_flow, validate_cash_flow

# Only these files can enter a release. No arbitrary directory traversal/copying.
ARTIFACTS = (
    "profile.json", "validation_summary.json", "validation_errors.json", "journal_balances.json",
    "general_ledger.csv", "ledger_summary.json", "monthly_trial_balance.csv", "trial_balance_summary.json",
    "reconciliation_details.json", "reconciliation_source_errors.json", "reconciliation_summary.json",
    "income_statement.csv", "income_statement_summary.json", "balance_sheet.csv", "balance_sheet_summary.json",
    "statement_accounts.csv", "statement_accounts_summary.json", "statement_mapping.json", "statements_summary.json",
    "statement_observations.json", "statement_validation_checks.json", "statement_validation_summary.json") + CASH_FLOW_ARTIFACTS


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(value)
    temporary.replace(path)


def table(rows, fields, labels=None):
    if not rows:
        return '<p class="empty">표시할 데이터가 없습니다.</p>'
    labels = labels or {}
    head = ''.join(f'<th scope="col">{escape(labels.get(k, k))}</th>' for k in fields)
    body = []
    for row in rows:
        cells = []
        for field in fields:
            value = row.get(field, "—")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            text = str(value)
            # Keep identifiers/date strings verbatim, format only decimal amounts.
            if isinstance(value, str) and '.' in value:
                try:
                    number = Decimal(value)
                    if number.is_finite():
                        text = format(number, ',.2f')
                except InvalidOperation:
                    pass
            cells.append(f'<td>{escape(text)}</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    return '<div class="scroll"><table><thead><tr>' + head + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>'


LABELS = {
    "company_id": "회사 ID", "period_start": "시작일", "period_end": "기준일", "basis": "기준",
    "operating_revenue": "영업수익", "cost_of_goods_sold": "매출원가", "gross_profit": "매출총이익",
    "operating_expense": "영업비용", "operating_profit": "영업이익", "other_revenue": "기타수익", "net_income": "순이익",
    "current_assets": "유동자산", "noncurrent_assets": "비유동자산", "total_assets": "자산 합계",
    "total_liabilities": "부채 합계", "recorded_equity": "장부상 자본", "prior_unclosed_earnings": "이전 미마감 손익",
    "current_fiscal_year_profit": "당기 누적손익", "total_equity": "자본 합계",
    "total_liabilities_and_equity": "부채·자본 합계", "account_id": "계정 ID", "status": "상태",
    "debit_difference": "차변 차이", "credit_difference": "대변 차이", "net_difference": "순액 차이",
    "candidate_descriptions": "차이를 설명하는 월말 분개", "candidate_line_ids": "원천 라인 ID",
    "section": "항목", "amount": "금액", "note": "확인 사항", "check": "검증 항목", "difference": "차이"}
LABELS.update({'beginning_cash': '기초 현금', 'ending_cash': '기말 현금', 'net_change': '현금 증감',
    'operating_net': '영업활동 순현금', 'investing_net': '투자활동 순현금', 'financing_net': '재무활동 순현금',
    'cash_and_cash_equivalents': '현금 및 현금성자산',
    **{a + '_' + s: label + suffix for a, label in [('operating', '영업'), ('investing', '투자'), ('financing', '재무')]
       for s, suffix in [('inflows', ' 유입'), ('outflows', ' 유출')]}})


def render_html(snapshot, output, reconciliation_mode="strict"):
    """Render drafts even with failed/missing checks; never authorizes publication."""
    output = Path(output)
    if reconciliation_mode not in {'strict', 'adjusted'}:
        raise ValidationError('Unknown reconciliation mode')
    artifacts = ARTIFACTS + EXTRA_ARTIFACTS if reconciliation_mode == 'adjusted' else ARTIFACTS
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    def read(name, default):
        try:
            value = json.loads((output / name).read_text())
            if not isinstance(value, type(default)):
                raise ValueError('Unexpected JSON structure')
            return value
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            return default
    income, balance, cash_flow = [], [], []
    for stage, target in (("income_statement", income), ("balance_sheet", balance), ('cash_flow', cash_flow)):
        try:
            target.extend(read_artifact(snapshot, output, stage))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{stage}: {exc}")
    summaries = {name: read(name, {}) for name in
                 ("validation_summary.json", "trial_balance_summary.json", "reconciliation_summary.json", "statement_validation_summary.json", 'cash_flow_validation_summary.json')}
    recon = read("reconciliation_details.json", [])
    source_errors = read("reconciliation_source_errors.json", [])
    journal_errors = read("validation_errors.json", [])
    observations = read("statement_observations.json", [])
    checks = read("statement_validation_checks.json", [])
    cash_checks = read('cash_flow_validation_checks.json', [])
    statement_summary = read("statements_summary.json", {})
    correction_summary, correction_log = {}, []
    if reconciliation_mode == 'adjusted':
        correction_summary = read('reconciliation_analysis_summary.json', {})
        correction_log = read('correction_log.json', [])
        summaries['adjusted_source_summary.json'] = read('adjusted_source_summary.json', {})
        summaries['adjusted_reconciliation_summary.json'] = read('adjusted_reconciliation_summary.json', {})
    required = [s for name, s in summaries.items() if reconciliation_mode == 'strict' or name != 'reconciliation_summary.json']
    passed = (all(s.get("status") == "passed" for s in required) and not errors and
              (reconciliation_mode == 'strict' or correction_summary.get('status') == 'eligible') and
              statement_summary.get('status') == 'draft' and statement_summary.get('unmapped_account_count') == 0)
    labels = {"validation_summary.json": "분개 검증", "trial_balance_summary.json": "시산표 검증",
              "reconciliation_summary.json": "원천 대사", "statement_validation_summary.json": "재무제표 교차 검증", 'cash_flow_validation_summary.json': '현금흐름표 검증'}
    if reconciliation_mode == 'adjusted':
        labels.update({'reconciliation_summary.json': '원천 대사 (보정 전·원본 보존)',
                       'adjusted_source_summary.json': '보정 근거·분개 완전성 검증',
                       'adjusted_reconciliation_summary.json': '보정 시산표 대사'})
    status_rows = ''.join('<tr><th scope="row">' + escape(labels[name]) + '</th><td class="' +
                          ('ok' if summary.get('status') == 'passed' else 'bad') + '">' +
                          ('통과' if summary.get('status') == 'passed' else '실패 / 미완료') + '</td></tr>'
                          for name, summary in summaries.items())
    present = {name: sha(output / name) for name in artifacts if (output / name).is_file()}
    links = ''.join(f'<li><a href="{escape(name, quote=True)}">{escape(name)}</a></li>' for name in present)
    try:
        manifest = json.loads((Path(snapshot) / "manifest.json").read_text())
        tables, _ = load(snapshot)
        companies = ', '.join(r.get('name', r['id']) for r in tables['companies'])
    except (OSError, ValueError, KeyError) as exc:
        manifest, companies = {}, '확인 불가'
        errors.append(f"source: {exc}")
        passed = False
    differences = [r for r in recon if r.get('status') not in {'matched', 'omitted_zero'}]
    sections = [
        ('income', '손익계산서', 'monthly는 당월, fiscal_ytd는 회계연도 누적입니다. 금액은 원천 통화 단위이며 세금·조정액을 추정하지 않습니다.',
         table(income, ['company_id', 'period_start', 'period_end', 'basis', 'operating_revenue', 'cost_of_goods_sold', 'gross_profit', 'operating_expense', 'operating_profit', 'other_revenue', 'net_income'], LABELS)),
        ('balance', '재무상태표', '기준일 누적잔액입니다. 장부상 자본과 미마감 손익을 분리하고 차감계정 부호를 보존합니다.',
         table(balance, ['company_id', 'period_end', 'cash_and_cash_equivalents', 'current_assets', 'noncurrent_assets', 'total_assets', 'total_liabilities', 'recorded_equity', 'prior_unclosed_earnings', 'current_fiscal_year_profit', 'total_equity', 'total_liabilities_and_equity'], LABELS)),
        ('cash-flow', '현금흐름표 (직접법)', f'영업·투자·재무활동으로 분류한 월별 및 회계연도 누적 현금흐름입니다. 은행 간 내부 이체와 비현금 거래는 제외합니다. 기말 현금은 시산표·재무상태표 현금과 연결합니다. 현금흐름 검증 {len(cash_checks):,}개, 실패 {sum(not c.get("passed", False) for c in cash_checks):,}개.',
         table(cash_flow, ['company_id', 'period_start', 'period_end', 'basis', 'beginning_cash', 'operating_inflows', 'operating_outflows', 'operating_net', 'investing_net', 'financing_net', 'net_change', 'ending_cash'], LABELS) +
         '<details><summary>현금흐름 검증 실패 상세</summary>' + table([c for c in cash_checks if not c.get('passed')], ['company_id', 'period_end', 'check', 'actual', 'expected', 'difference'], LABELS) + '</details>'),
        ('reconciliation', '원천 시산표 대사', f"차이 {len(differences):,}행 · 원천 오류 {len(source_errors):,}건. 차이는 계산값 − 원천값입니다. 원천 누적 차변·대변을 각각 비교하며 허용 오차는 0입니다.",
         table(differences, ['company_id', 'period_end', 'account_id', 'status', 'debit_difference', 'credit_difference', 'net_difference', 'candidate_descriptions', 'candidate_line_ids'], LABELS) +
         ('<details><summary>원천 오류</summary>' + table(source_errors, ['source_id', 'key', 'reason']) + '</details>' if source_errors else '')),
        ('checks', '교차 검증 상세', f"검증 {len(checks):,}개 · 실패 {sum(not c.get('passed', False) for c in checks):,}개. 전체 결과는 JSON으로 제공합니다.",
         table([c for c in checks if not c.get('passed')], ['company_id', 'period_end', 'check', 'actual', 'expected', 'difference'], LABELS)),
        ('observations', '원천 범위와 확인 사항', '최초 제공 분개 이전의 기초잔액은 0으로 가정합니다. 음수 자산과 누락 가능성은 자동 보정하지 않습니다. 내부 검증 통과는 거래의 경제적 타당성에 대한 보증이 아닙니다.',
         table(observations, ['company_id', 'period_end', 'section', 'amount', 'note'], LABELS))]
    if reconciliation_mode == 'adjusted':
        sections.insert(3, ('corrections', '원인 분석과 별도 보정',
            f"보정 대상 {len(correction_log)}행 · 근거 전표 {correction_summary.get('evidence_journal_count', 0)}건. 원천 CSV와 보정 전 실패 결과는 그대로 보존합니다. 월별 원천 누적액에 그 달의 누락 감가상각만 더하며 이전 달 보정을 다시 누적하지 않습니다. 생성 시점 차이는 데이터 패턴에 따른 추정입니다.",
            table(correction_summary.get('periods', []), ['company_id', 'period_end', 'debit', 'credit', 'journal_count', 'first_depreciation_entry_number', 'last_other_entry_number', 'depreciation_follows_other_entries'], LABELS) +
            '<details><summary>보정 행별 원금액·추가액·근거 라인</summary>' + table(correction_log, ['period_end', 'account_id', 'operation', 'original_debit', 'original_credit', 'adjustment_debit', 'adjustment_credit', 'adjusted_debit', 'adjusted_credit', 'evidence_line_ids'], LABELS) + '</details>'))
    content = ''.join(f'<section id="{anchor}"><h2>{escape(title)}</h2><p>{escape(note)}</p>{body}</section>' for anchor, title, note, body in sections)
    diagnostics = '<pre>' + escape(json.dumps({'report_errors': errors, 'journal_errors': journal_errors}, ensure_ascii=False, indent=2)) + '</pre>' if errors or journal_errors else ''
    html = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>회계 파이프라인 재무 보고서</title><style>
:root{color-scheme:light;--ink:#172b3a;--muted:#526575;--line:#dae1e4;--paper:#fff;--bg:#f3f5f4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,sans-serif}main{max-width:1440px;margin:auto;padding:36px 24px}header{padding:28px;background:#142e3d;color:#fff;border-radius:12px}h1{font-size:30px;margin:6px 0}h2{font-size:22px;margin:0 0 10px}header p{color:#d3e1e7;overflow-wrap:anywhere}section{margin-top:24px;padding:24px;background:var(--paper);border:1px solid var(--line);border-radius:10px}p{color:var(--muted)}a{color:#145776}nav{display:flex;flex-wrap:wrap;gap:18px;margin-top:20px}.badge{display:inline-block;padding:5px 12px;border-radius:5px;background:#fff0c3;color:#624700;font-weight:700}.alert{padding:14px 18px;border-left:4px solid #b44132;background:#fff0ec}.ok{color:#116447;font-weight:700}.bad{color:#a43326;font-weight:700}.scroll{overflow:auto;max-height:620px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}thead th{position:sticky;top:0;background:#eaf0f1;white-space:nowrap}td{font-variant-numeric:tabular-nums}tr:nth-child(even){background:#f8faf9}pre{white-space:pre-wrap;overflow-wrap:anywhere}.downloads{columns:2}.empty{font-style:italic}footer{padding:24px 0;color:var(--muted)}@media(max-width:700px){main{padding:16px 10px}section{padding:16px}.downloads{columns:1}}@media print{body{background:white}main{padding:0}header{color:#172b3a;background:white;border:1px solid #bbb}header p{color:#526575}.scroll{max-height:none;overflow:visible}nav{display:none}section{break-inside:auto}thead{display:table-header-group}thead th{position:static}table{font-size:9px}th,td{padding:4px}}
</style></head><body><main>'''
    html += '<header><span class="badge" id="publication-badge">검토용 초안</span><h1>재무제표와 검증 결과</h1>'
    html += '<p>' + escape(companies) + '</p><p>원천: ' + escape(str(manifest.get('dataset', '확인 불가'))) + '<br>Revision: ' + escape(str(manifest.get('revision', '확인 불가'))) + '</p></header>'
    if manifest.get('scope'):
        html += '<p class="alert">대상 월: ' + escape(manifest['scope']['period']) + ' · 기준일: ' + escape(manifest['scope']['as_of']) + ' · 기초잔액 계산을 위해 이전 분개를 포함하며 이후 거래는 제외합니다.</p>'
    if reconciliation_mode == 'adjusted':
        html += '<p class="alert">별도 보정 시산표 기준 · 원본 대사 실패 이력은 보존하며, 발행 조건은 보정 근거 검증 및 보정 후 대사 통과입니다.</p>'
    html += '<nav aria-label="보고서 목차">' + ''.join(f'<a href="#{a}">{escape(t)}</a>' for a, t, _, _ in sections) + '<a href="#downloads">산출물</a></nav>'
    html += '<section><h2>발행 전 품질 상태</h2><p class="alert">' + ('기록된 검증이 모두 통과했습니다. 최종 발행은 현재 산출물 재검증 후에만 수행합니다.' if passed else '최종 발행 차단: 실패하거나 완료되지 않은 검증이 있습니다. 이 보고서는 검토용 초안입니다.') + '</p>'
    html += '<table><tbody>' + status_rows + '</tbody></table><p>미매핑 계정: ' + escape(str(statement_summary.get('unmapped_account_count', '미확인'))) + '</p>' + diagnostics + '</section>'
    html += content + '<section id="downloads"><h2>산출물 다운로드</h2><ul class="downloads">' + links + '</ul></section>'
    html += '<footer>데이터: Mindweave Technologies · CC-BY-NC-4.0 · 원천 분개 범위의 재구성. 이 보고서는 원천 데이터의 적정성에 대한 외부 감사 의견이 아닙니다.</footer></main></body></html>'
    write_text(output / 'report.html', html)
    write_json(output / 'html_report_summary.json', {'status': 'draft', 'report_sha256': sha(output / 'report.html'),
               'snapshot_manifest_sha256': fingerprint(snapshot), 'artifacts': present, 'render_errors': errors,
               'reconciliation_mode': reconciliation_mode})
    return str(output / 'report.html')


def publish_results(snapshot, output, published_root, reconciliation_mode="strict"):
    """Rebuild current source independently; atomically expose a full release only.

    A failed attempt never changes an earlier release or latest.json.
    """
    output, published_root = Path(output), Path(published_root)
    try:
        scope = json.loads((Path(snapshot) / 'manifest.json').read_text()).get('scope')
        if scope:
            from accounting.periods import cutoff
            cutoff(scope['period'])
            published_root = published_root / 'periods' / scope['period']
        if reconciliation_mode not in {'strict', 'adjusted'}:
            raise ValidationError('Unknown reconciliation mode')
        artifacts = ARTIFACTS + EXTRA_ARTIFACTS if reconciliation_mode == 'adjusted' else ARTIFACTS
        report = json.loads((output / 'html_report_summary.json').read_text())
        if report.get('reconciliation_mode', 'strict') != reconciliation_mode:
            raise ValidationError('Report reconciliation mode does not match publication mode')
        if report.get('render_errors') or report.get('status') != 'draft':
            raise ValidationError('HTML report has rendering errors or is incomplete')
        if report['snapshot_manifest_sha256'] != fingerprint(snapshot) or report['report_sha256'] != sha(output / 'report.html'):
            raise ValidationError('HTML report is stale or changed')
        if set(report['artifacts']) != set(artifacts):
            raise ValidationError('Required report artifacts are missing')
        for name, checksum in report['artifacts'].items():
            if sha(output / name) != checksum:
                raise ValidationError(f'Artifact changed after HTML rendering: {name}')
        # A forged "passed" summary cannot bypass this gate. Recompute every stage
        # from checksummed source and compare the complete deliverable set.
        with tempfile.TemporaryDirectory(dir=output.parent, prefix='.verify-') as temporary:
            verified = Path(temporary)
            profile(snapshot, verified)
            validate(snapshot, verified)
            build_general_ledger(snapshot, verified)
            build_trial_balance(snapshot, verified)
            try:
                reconcile_trial_balance(snapshot, verified)
            except ValidationError:
                if reconciliation_mode == 'strict':
                    raise
            if reconciliation_mode == 'adjusted':
                build_adjusted_trial_balance(snapshot, verified)
                reconcile_adjusted_trial_balance(snapshot, verified)
            build_statements(snapshot, verified, output / 'statement_mapping.json')
            validate_statements(snapshot, verified)
            build_cash_flow(snapshot, verified)
            validate_cash_flow(snapshot, verified)
            for name in artifacts:
                if sha(verified / name) != report['artifacts'][name]:
                    raise ValidationError(f'Artifact does not match independently verified source: {name}')
        # Regenerate rather than trusting the draft HTML, then label it final.
        # The original remains a draft; the release has a separate final report.
        with tempfile.TemporaryDirectory(dir=output.parent, prefix='.release-') as temporary:
            bundle = Path(temporary) / 'bundle'
            bundle.mkdir()
            for name in artifacts:
                shutil.copyfile(output / name, bundle / name)
                if sha(bundle / name) != report['artifacts'][name]:
                    raise ValidationError(f'Artifact changed during publication: {name}')
            render_html(snapshot, bundle, reconciliation_mode)
            final_html = (bundle / 'report.html').read_text().replace(
                '<span class="badge" id="publication-badge">검토용 초안</span>',
                '<span class="badge" id="publication-badge">최종 발행</span>').replace(
                '기록된 검증이 모두 통과했습니다. 최종 발행은 현재 산출물 재검증 후에만 수행합니다.',
                '보정 근거와 보정 후 대사를 포함한 검증을 독립적으로 재실행한 뒤 최종 발행했습니다.' if reconciliation_mode == 'adjusted' else '모든 검증과 원천 대사를 독립적으로 재검증한 뒤 최종 발행했습니다.')
            write_text(bundle / 'report.html', final_html)
            # Draft receipt is not part of the release; final manifest is authoritative.
            (bundle / 'html_report_summary.json').unlink()
            shutil.copyfile(Path(snapshot) / 'manifest.json', bundle / 'source_manifest.json')
            if sha(bundle / 'source_manifest.json') != report['snapshot_manifest_sha256']:
                raise ValidationError('Source manifest changed during publication')
            files = {p.name: sha(p) for p in sorted(bundle.iterdir())}
            payload = {'format_version': 2, 'period': scope['period'] if scope else None,
                       'reconciliation_mode': reconciliation_mode, 'snapshot_manifest_sha256': fingerprint(snapshot), 'files': files}
            release_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            manifest = {**payload, 'release_id': release_id, 'status': 'published',
                        'reconciliation_mode': reconciliation_mode,
                        'period': scope['period'] if scope else None,
                        'quality_gate': ('raw differences preserved; evidence-based correction and adjusted-source reconciliation independently passed' if reconciliation_mode == 'adjusted' else 'source reconstruction, journal validation, source reconciliation and statement checks all passed') + '; cash-flow classification, source tie-outs and balance-sheet cash checks passed'}
            write_json(bundle / 'release_manifest.json', manifest)
            published_root.mkdir(parents=True, exist_ok=True)
            destination = published_root / release_id
            if destination.exists():
                existing = json.loads((destination / 'release_manifest.json').read_text())
                if existing != manifest or {p.name for p in destination.iterdir()} != set(files) | {'release_manifest.json'}:
                    raise ValidationError('Existing release manifest mismatch')
                for name, checksum in files.items():
                    if sha(destination / name) != checksum:
                        raise ValidationError(f'Existing release is corrupted: {name}')
            else:
                # Stage within publication filesystem so rename stays atomic.
                with tempfile.TemporaryDirectory(dir=published_root, prefix='.staging-') as stage:
                    staged = Path(stage) / 'bundle'
                    shutil.copytree(bundle, staged)
                    staged.rename(destination)
            write_json(output / 'publication_summary.json', {'status': 'published', 'release_id': release_id, 'path': str(destination), 'reconciliation_mode': reconciliation_mode})
            # Commit the public pointer last: no fallible metadata writes follow it.
            write_json(published_root / 'latest.json', {'release_id': release_id, 'report': f'{release_id}/report.html'})
            return str(destination / 'report.html')
    except Exception as exc:
        reason = str(exc)
        if 'verified' in locals():
            reason = reason.replace(str(verified), str(output))
        write_json(output / 'publication_summary.json', {'status': 'blocked', 'reason': reason})
        raise ValidationError(f'Final publication blocked: {reason}') from exc

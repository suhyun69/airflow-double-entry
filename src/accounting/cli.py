import argparse
import json
import uuid
from pathlib import Path
from accounting.pipeline import REVISION, ValidationError, collect, profile, validate
from accounting.ledger import build_general_ledger, build_trial_balance, reconcile_trial_balance
from accounting.statements import build_statements, validate_statements
from accounting.reporting import render_html, publish_results
from accounting.corrections import build_adjusted_trial_balance, reconcile_adjusted_trial_balance
from accounting.cash_flow import build_cash_flow, validate_cash_flow
from accounting.periods import NoDataPeriod, create_period_snapshot
from accounting.runs import RunStore


def main():
    stages = ['validate', 'ledger', 'trial-balance', 'reconcile', 'statements', 'cross-validate', 'report', 'publish']
    parser = argparse.ArgumentParser(description='Accounting pipeline with isolated monthly runs and execution history')
    parser.add_argument('--root', default='data')
    parser.add_argument('--revision', default=REVISION)
    parser.add_argument('--snapshot', help='Reuse a checksummed local source snapshot')
    parser.add_argument('--period', help='As-of accounting month YYYY-MM; includes history through month-end')
    parser.add_argument('--until', choices=stages, default='publish')
    parser.add_argument('--mapping', help='Optional version-1 account mapping JSON')
    parser.add_argument('--published-root')
    parser.add_argument('--reconciliation-mode', choices=['strict', 'adjusted'], default='strict')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    store = RunStore(root, 'cli-' + uuid.uuid4().hex)
    store.start(kind='cli', period=args.period or 'full', mode=args.reconciliation_mode)
    last, errors, snapshot, output, path = stages.index(args.until), [], None, None, None
    def run(name, fn, *a):
        with store.task(name):
            return fn(*a)
    def prepare():
        source = args.snapshot or collect(root, args.revision)
        scoped = create_period_snapshot(source, root, args.period) if args.period else source
        folder = root / 'reports' / Path(source).name / (args.period or 'full') / store.key
        store.configure(source=str(source), snapshot=str(scoped), output=str(folder))
        return scoped, folder
    try:
        snapshot, output = run('prepare_run', prepare)
        run('profile_source', profile, snapshot, output)
        path = run('validate_journals', validate, snapshot, output)
        if last >= 1:
            run('build_general_ledger', build_general_ledger, snapshot, output)
            path = output / 'ledger_summary.json'
        if last >= 2:
            run('build_trial_balance', build_trial_balance, snapshot, output)
            path = output / 'trial_balance_summary.json'
        if last >= 3:
            def reconcile():
                try:
                    return reconcile_trial_balance(snapshot, output)
                except ValidationError:
                    if args.reconciliation_mode == 'strict':
                        raise
            try:
                path = run('reconcile_trial_balance', reconcile)
            except ValidationError as exc:
                errors.append(str(exc))
            if args.reconciliation_mode == 'adjusted':
                run('build_adjusted_trial_balance', build_adjusted_trial_balance, snapshot, output)
                path = run('reconcile_adjusted_trial_balance', reconcile_adjusted_trial_balance, snapshot, output)
        if last >= 4:
            path = run('build_statements', build_statements, snapshot, output, args.mapping)
            run('build_cash_flow', build_cash_flow, snapshot, output)
        if last >= 5:
            path = run('validate_statements', validate_statements, snapshot, output)
            run('validate_cash_flow', validate_cash_flow, snapshot, output)
    except NoDataPeriod as exc:
        store.configure(no_data=str(exc))
    except Exception as exc:
        errors.append(str(exc))
    if snapshot and output and last >= 6:
        try:
            path = run('render_html', render_html, snapshot, output, args.reconciliation_mode)
            if last >= 7:
                published = Path(args.published_root) if args.published_root else root / 'published'
                path = run('publish_results', publish_results, snapshot, output, published, args.reconciliation_mode)
        except Exception as exc:
            errors.append(str(exc))
    try:
        record = store.finish(require_publication=last >= 7)
    except ValidationError as exc:
        errors.append(str(exc))
        record = store.read()
    print(json.dumps({'state': record['state'], 'period': record.get('period'), 'report': str(path) if path else None,
                      'history': str(store.path), 'history_dashboard': str(root / 'runs' / 'index.html')}, ensure_ascii=False, indent=2))
    if errors:
        parser.exit(1, '\n'.join(dict.fromkeys(errors)) + '\n')


if __name__ == '__main__':
    main()

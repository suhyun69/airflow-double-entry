"""Real-data full/monthly/no-data integration, scheduler interval and run-history checks."""
import json
import os
import runpy
from pathlib import Path
import pendulum
from airflow.timetables.base import TimeRestriction
from accounting.runs import RunStore
from accounting.reporting import sha

mode = os.environ.get('ACCOUNTING_RECONCILIATION_MODE', 'strict')
period = os.environ.get('ACCOUNTING_TEST_PERIOD')
root = Path(os.environ.get('ACCOUNTING_DATA_ROOT', '/opt/airflow/data'))
dag = runpy.run_path('/opt/airflow/dags/accounting_pipeline.py')['accounting_quality_pipeline']()
assert len(dag.task_ids) == 15, dag.task_ids
assert {task.task_id for task in dag.leaves} == {'finalize_run'}
interval = dag.timetable.next_dagrun_info(last_automated_data_interval=None,
    restriction=TimeRestriction(earliest=pendulum.datetime(2024, 1, 1, tz='Asia/Seoul'),
                                latest=pendulum.datetime(2024, 2, 1, tz='Asia/Seoul'), catchup=True))
assert interval.data_interval.start.in_timezone('Asia/Seoul').isoformat().startswith('2024-01-01T00:00:00'), interval
assert interval.run_after.in_timezone('Asia/Seoul').isoformat().startswith('2024-02-01T00:00:00'), interval
full_latest = root / 'published' / 'latest.json'
before_latest = full_latest.read_bytes() if full_latest.exists() else None
conf = {'reconciliation_mode': mode}
if period:
    conf['period'] = period
result = dag.test(run_conf=conf)
record = RunStore(root, result.run_id).read()
if record['state'] == 'no_data':
    assert result.state == 'success', result.state
    assert not record.get('publication'), record
    assert period and period > '2025-12', record
else:
    assert result.state == ('success' if mode == 'adjusted' else 'failed'), result.state
    assert record['state'] == ('success' if mode == 'adjusted' else 'failed'), record
    output = Path(record['output'])
    report = json.loads((output / 'reconciliation_summary.json').read_text())
    n = (int(period[:4]) - 2024) * 12 + int(period[5:]) if period else 24
    assert report['status'] == 'failed' and report['difference_count'] == 4 * n, report
    cash = json.loads((output / 'cash_flow_validation_summary.json').read_text())
    assert cash['status'] == 'passed' and cash['row_count'] == 2 * n, cash
    if mode == 'adjusted':
        assert record['publication']['status'] == 'published', record
        release = Path(record['publication']['path'])
        manifest = json.loads((release / 'release_manifest.json').read_text())
        assert manifest['period'] == period, manifest
        for name, checksum in manifest['files'].items():
            assert sha(release / name) == checksum, name
        if period:
            import csv
            with (release / 'cash_flow_statement.csv').open() as f:
                rows = list(csv.DictReader(f))
            assert max(r['period_end'] for r in rows)[:7] == period
            assert (root / 'published' / 'periods' / period / 'latest.json').exists()
    else:
        assert any(f['task'] == 'reconcile_trial_balance' for f in record['failures']), record
        assert any('Reconciliation failed' in t.get('traceback', '') for t in record['tasks']), record
if period or mode == 'strict':
    assert before_latest == (full_latest.read_bytes() if full_latest.exists() else None), 'Full-period latest was changed'
print('Verified:', {'mode': mode, 'period': period, 'dag_state': str(result.state), 'history_state': record['state'],
                    'run_id': result.run_id, 'output': record.get('output'), 'failure_tasks': [f['task'] for f in record.get('failures', [])]})

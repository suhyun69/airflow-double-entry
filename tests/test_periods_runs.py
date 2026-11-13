import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import test_reporting as fixtures
from accounting.periods import NoDataPeriod, create_period_snapshot, execution_period
from accounting.pipeline import ValidationError, load, profile, write_json
from accounting.ledger import build_general_ledger, build_trial_balance, reconcile_trial_balance, write_csv
from accounting.statements import build_statements, validate_statements
from accounting.cash_flow import build_cash_flow, validate_cash_flow
from accounting.reporting import render_html, publish_results, sha
from accounting.runs import RunStore


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.r = fixtures.ReportingTests()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.f = self.r.f
        self.root = self.f.snapshot.parent / 'period-work'
        for name, rows in self.f.tables.items():
            self.f.tables[name] = [r for r in rows if r.get('company_id') != 'other' and r.get('id') != 'other' and not r.get('journal_entry_id', '').startswith('other-')]
        self.f.save()

    def test_asof_excludes_future_but_retains_opening_history(self):
        before = {p.name: sha(p) for p in self.f.snapshot.iterdir()}
        scoped = create_period_snapshot(self.f.snapshot, self.root, '2024-02')
        tables, _ = load(scoped)
        self.assertNotIn('reversal', {r['id'] for r in tables['journal_entries']})
        self.assertIn('opening', {r['id'] for r in tables['journal_entries']})
        self.assertTrue(all(r['period_end'] <= '2024-02-29' for r in tables['trial_balances']))
        self.assertEqual(before, {p.name: sha(p) for p in self.f.snapshot.iterdir()})
        self.assertEqual(create_period_snapshot(self.f.snapshot, self.root, '2024-02'), scoped)

    def test_airflow_enum_types_and_seoul_month_boundaries(self):
        from enum import Enum
        from datetime import datetime, timezone
        class Kind(str, Enum):
            SCHEDULED = 'scheduled'
            BACKFILL = 'backfill'
            MANUAL = 'manual'
        start = datetime(2024, 1, 31, 15, tzinfo=timezone.utc)
        self.assertEqual(execution_period({}, Kind.SCHEDULED, start), '2024-02')
        self.assertEqual(execution_period({}, Kind.BACKFILL, start), '2024-02')
        self.assertIsNone(execution_period({}, Kind.MANUAL, start))
        self.assertEqual(execution_period({'period': '2024-01'}, Kind.MANUAL, start), '2024-01')
        with self.assertRaises(ValidationError):
            execution_period({}, 'unknown', start)

    def test_monthly_publication_does_not_replace_full_or_other_period(self):
        self.s = self.r.s
        self.s.mapping()
        full_latest = self.root / 'published' / 'latest.json'
        write_json(full_latest, {'release_id': 'previous-full'})
        for month in ('2024-02', '2024-01'):
            scoped = create_period_snapshot(self.f.snapshot, self.root, month)
            out = self.root / 'reports' / month
            profile(scoped, out)
            build_general_ledger(scoped, out)
            build_trial_balance(scoped, out)
            reconcile_trial_balance(scoped, out)
            build_statements(scoped, out, self.s.mapping_path)
            validate_statements(scoped, out)
            build_cash_flow(scoped, out)
            validate_cash_flow(scoped, out)
            render_html(scoped, out)
            published = Path(publish_results(scoped, out, self.root / 'published'))
            self.assertTrue(published.is_file())
            self.assertIn(month, str(published))
            manifest = json.loads((published.parent / 'release_manifest.json').read_text())
            self.assertEqual(manifest['period'], month)
        self.assertEqual(json.loads(full_latest.read_text()), {'release_id': 'previous-full'})
        self.assertTrue((self.root / 'published' / 'periods' / '2024-02' / 'latest.json').exists())

    def test_no_coverage_invalid_month_and_missing_source_month(self):
        for month in ('2023-12', '2026-01'):
            with self.assertRaises(NoDataPeriod):
                create_period_snapshot(self.f.snapshot, self.root, month)
        for month in ('2024-13', '../2024', '2024-1'):
            with self.assertRaises(ValidationError):
                create_period_snapshot(self.f.snapshot, self.root, month)
        self.f.tables['trial_balances'] = [r for r in self.f.tables['trial_balances'] if r['period_end'] != '2024-02-29']
        self.f.save()
        with self.assertRaisesRegex(ValidationError, 'Missing source trial balance'):
            create_period_snapshot(self.f.snapshot, self.root, '2024-02')

    def test_forged_scoped_rows_even_with_new_checksum_are_rejected(self):
        scoped = Path(create_period_snapshot(self.f.snapshot, self.root, '2024-01'))
        tables, columns = load(scoped)
        tables['journal_entries'][0]['entry_date'] = '2025-01-01'
        write_csv(scoped / 'journal_entries.csv', columns['journal_entries'], tables['journal_entries'])
        manifest = json.loads((scoped / 'manifest.json').read_text())
        manifest['files']['journal_entries']['sha256'] = sha(scoped / 'journal_entries.csv')
        write_json(scoped / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValidationError, 'cutoff'):
            load(scoped)

    def test_scope_lineage_survives_relocation_between_host_and_container(self):
        import tempfile
        import shutil
        scoped = Path(create_period_snapshot(self.f.snapshot, self.root, '2024-01'))
        with tempfile.TemporaryDirectory() as temporary:
            relocated = Path(temporary) / 'data'
            shutil.copytree(self.f.snapshot.parent, relocated)
            moved = relocated / scoped.relative_to(self.f.snapshot.parent.resolve())
            tables, _ = load(moved)
            self.assertEqual(len(tables['journal_entries']), 2)


class RunTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root, 'test-run')
        self.store.start(period='2024-01', mode='adjusted')

    def test_retry_attempts_preserved_and_latest_success_wins(self):
        with self.assertRaises(ValueError):
            with self.store.task('extract', 1):
                raise ValueError('network unavailable')
        with self.store.task('extract', 2):
            pass
        record = self.store.finish(require_publication=False)
        self.assertEqual(record['state'], 'success')
        self.assertEqual([t['state'] for t in record['tasks']], ['failed', 'success'])
        self.assertIn('ValueError: network unavailable', record['tasks'][0]['traceback'])

    def test_failure_cannot_be_masked_by_successful_report(self):
        with self.assertRaises(ValueError):
            with self.store.task('validate'):
                raise ValueError('<script>bad amount</script>')
        with self.store.task('report'):
            pass
        with self.assertRaises(ValidationError):
            self.store.finish(require_publication=False)
        record = self.store.read()
        self.assertEqual(record['failures'][0]['task'], 'validate')
        self.assertNotIn('<script>', (self.root / 'runs' / 'index.html').read_text())

    def test_no_data_never_claims_publication(self):
        with self.assertRaises(NoDataPeriod):
            with self.store.task('prepare'):
                self.store.configure(no_data='outside coverage')
                raise NoDataPeriod('outside coverage')
        record = self.store.finish()
        self.assertEqual(record['state'], 'no_data')
        self.assertEqual(record['publication'], {})

    def test_concurrent_task_events_are_not_lost(self):
        def work(n):
            with self.store.task(f'task-{n}'):
                pass
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(work, range(24)))
        record = self.store.finish(require_publication=False)
        self.assertEqual(len(record['tasks']), 24)
        self.assertTrue(all(t['state'] == 'success' for t in record['tasks']))

    def test_incomplete_run_fails_without_a_published_result(self):
        with self.store.task('report'):
            pass
        with self.assertRaisesRegex(ValidationError, 'Publication incomplete'):
            self.store.finish()

    def test_new_generation_does_not_erase_old_attempts(self):
        with self.assertRaises(ValueError):
            with self.store.task('old'):
                raise ValueError('old failure')
        self.store.start(period='2024-02')
        with self.store.task('new'):
            pass
        self.assertEqual(self.store.finish(require_publication=False)['state'], 'success')
        self.assertEqual(len(self.store.read()['tasks']), 2)

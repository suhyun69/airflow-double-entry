import json
import unittest
from pathlib import Path

import test_reporting as fixtures
from accounting.corrections import (EXTRA_ARTIFACTS, analyze_reconciliation, build_adjusted_trial_balance,
                                    reconcile_adjusted_trial_balance)
from accounting.ledger import reconcile_trial_balance
from accounting.pipeline import ValidationError, profile, write_json
from accounting.reporting import publish_results, render_html, sha
from accounting.statements import validate_statements
from accounting.cash_flow import build_cash_flow, validate_cash_flow


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.r = fixtures.ReportingTests()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.f, self.s = self.r.f, self.r.s
        self.output = self.f.output
        next(a for a in self.f.tables['chart_of_accounts'] if a['id'] == 'c-expense')['code'] = '6-2800'
        self.s.add_account('accum', 'asset', 'fixed_asset')
        self.f.tables['chart_of_accounts'][-1]['code'] = '1-2010'
        for n, (day, value) in enumerate([('2024-01-31', '3'), ('2024-02-29', '3'), ('2024-03-31', '1')], 100):
            self.f.entry(f'dep{n}', day, 'expense', 'accum', value, number=n)
        for entry in self.f.tables['journal_entries']:
            dep = entry['id'].startswith('dep')
            entry.update(source_module='assets' if dep else 'sales', source_id='fixed-asset' if dep else '',
                         description='Depreciation — fixture asset' if dep else entry['id'])
        for row in self.f.tables['trial_balances']:
            if row['account_id'] == 'c-expense' and row['period_end'] == '2024-02-29':
                row['debit_balance'] = '23'
            if row['account_id'] == 'c-expense' and row['period_end'] == '2024-03-31':
                row['debit_balance'] = '26'
        self.f.source('2024-02-29', 'accum', '0', '3')
        self.f.source('2024-03-31', 'accum', '0', '6')

    def build(self):
        self.s.build()
        profile(self.f.snapshot, self.output)
        with self.assertRaises(ValidationError):
            reconcile_trial_balance(self.f.snapshot, self.output)
        validate_statements(self.f.snapshot, self.output)
        build_cash_flow(self.f.snapshot, self.output)
        validate_cash_flow(self.f.snapshot, self.output)

    def adjust(self):
        self.build()
        build_adjusted_trial_balance(self.f.snapshot, self.output)
        reconcile_adjusted_trial_balance(self.f.snapshot, self.output)

    def test_raw_preserved_and_each_month_only_adjusted_once(self):
        self.build()
        before = {p.name: p.read_bytes() for p in self.f.snapshot.iterdir()}
        original_report = (self.output / 'reconciliation_summary.json').read_bytes()
        build_adjusted_trial_balance(self.f.snapshot, self.output)
        reconcile_adjusted_trial_balance(self.f.snapshot, self.output)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.f.snapshot.iterdir()})
        self.assertEqual(original_report, (self.output / 'reconciliation_summary.json').read_bytes())
        rows = {r['period_end']: r for r in self.f.read('adjusted_source_trial_balances.csv') if r['account_id'] == 'c-accum'}
        self.assertEqual([rows[p]['credit_balance'] for p in sorted(rows)], ['3.00', '6.00', '7.00'])
        self.assertEqual(self.f.read('adjusted_reconciliation_summary.json')['status'], 'passed')
        self.assertEqual(self.f.read('adjusted_source_summary.json')['inserted_row_count'], 1)
        analysis = self.f.read('reconciliation_analysis_summary.json')
        self.assertEqual((analysis['evidence_journal_count'], analysis['evidence_line_count']), (3, 6))
        self.assertEqual((analysis['total_adjustment_debit'], analysis['total_adjustment_credit']), ('7.00', '7.00'))
        first = (self.output / 'adjusted_source_trial_balances.csv').read_bytes()
        build_adjusted_trial_balance(self.f.snapshot, self.output)
        self.assertEqual(first, (self.output / 'adjusted_source_trial_balances.csv').read_bytes())

    def test_adjusted_publication_includes_audit_but_strict_still_blocks(self):
        self.adjust()
        render_html(self.f.snapshot, self.output, 'adjusted')
        with self.assertRaisesRegex(ValidationError, 'mode'):
            publish_results(self.f.snapshot, self.output, self.r.published)
        path = Path(publish_results(self.f.snapshot, self.output, self.r.published, 'adjusted'))
        manifest = json.loads((path.parent / 'release_manifest.json').read_text())
        self.assertEqual(manifest['reconciliation_mode'], 'adjusted')
        self.assertTrue(set(EXTRA_ARTIFACTS) <= set(manifest['files']))
        self.assertEqual(json.loads((path.parent / 'reconciliation_summary.json').read_text())['status'], 'failed')
        self.assertEqual(json.loads((path.parent / 'adjusted_reconciliation_summary.json').read_text())['status'], 'passed')
        self.assertIn('원본 대사 실패 이력은 보존', path.read_text())
        self.assertEqual(publish_results(self.f.snapshot, self.output, self.r.published, 'adjusted'), str(path))
        latest = (self.r.published / 'latest.json').read_bytes()
        render_html(self.f.snapshot, self.output)
        with self.assertRaisesRegex(ValidationError, 'Reconciliation failed'):
            publish_results(self.f.snapshot, self.output, self.r.published)
        self.assertEqual(latest, (self.r.published / 'latest.json').read_bytes())

    def test_unrelated_difference_cannot_be_plugged(self):
        self.f.tables['trial_balances'][0]['debit_balance'] = '999'
        self.build()
        with self.assertRaisesRegex(ValidationError, 'blocked'):
            build_adjusted_trial_balance(self.f.snapshot, self.output)
        self.assertGreater(self.f.read('reconciliation_analysis_summary.json')['unexplained_difference_count'], 0)

    def test_unknown_module_and_account_are_not_eligible(self):
        next(e for e in self.f.tables['journal_entries'] if e['id'] == 'dep100')['source_module'] = 'other'
        self.build()
        with self.assertRaises(ValidationError):
            build_adjusted_trial_balance(self.f.snapshot, self.output)

    def test_partial_counterpart_is_blocked_even_if_one_side_explains(self):
        # Source expense already includes January depreciation; contra does not.
        next(r for r in self.f.tables['trial_balances'] if r['account_id'] == 'c-expense' and r['period_end'] == '2024-01-31')['debit_balance'] = '23'
        self.build()
        with self.assertRaises(ValidationError):
            build_adjusted_trial_balance(self.f.snapshot, self.output)
        self.assertFalse(self.f.read('reconciliation_analysis_summary.json')['complete_journals'])

    def test_partial_batch_and_incorrect_amount_are_blocked(self):
        next(r for r in self.f.tables['trial_balances'] if r['account_id'] == 'c-expense' and r['period_end'] == '2024-01-31')['debit_balance'] = '21'
        self.build()
        with self.assertRaises(ValidationError):
            build_adjusted_trial_balance(self.f.snapshot, self.output)

    def test_source_duplicate_key_blocks_correction(self):
        self.f.source('2024-01-31', 'cash', '100', '20')
        self.build()
        with self.assertRaises(ValidationError):
            build_adjusted_trial_balance(self.f.snapshot, self.output)
        self.assertGreater(self.f.read('reconciliation_analysis_summary.json')['source_error_count'], 0)

    def test_forged_correction_audit_cannot_bypass_independent_gate(self):
        self.adjust()
        log = self.f.read('correction_log.json')
        log[0]['adjustment_debit'] = '999.00'
        write_json(self.output / 'correction_log.json', log)
        render_html(self.f.snapshot, self.output, 'adjusted')
        with self.assertRaisesRegex(ValidationError, 'independently verified source'):
            publish_results(self.f.snapshot, self.output, self.r.published, 'adjusted')
        self.assertFalse((self.r.published / 'latest.json').exists())

    def test_adjusted_file_mutation_is_detected(self):
        self.adjust()
        with (self.output / 'adjusted_source_trial_balances.csv').open('a') as f:
            f.write('changed')
        with self.assertRaisesRegex(ValueError, 'Checksum'):
            reconcile_adjusted_trial_balance(self.f.snapshot, self.output)

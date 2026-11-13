import json
import unittest
from pathlib import Path

import test_reporting as fixtures
from accounting.cash_flow import build_cash_flow, validate_cash_flow, FIELDS, DETAIL_FIELDS, EXCLUSION_FIELDS
from accounting.ledger import receipt, write_csv
from accounting.pipeline import ValidationError, profile, write_json
from accounting.reporting import render_html, publish_results


class CashFlowTests(unittest.TestCase):
    def setUp(self):
        self.r = fixtures.ReportingTests()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.f, self.s = self.r.f, self.r.s
        self.output = self.f.output

    def build(self):
        self.s.build()
        build_cash_flow(self.f.snapshot, self.output)
        validate_cash_flow(self.f.snapshot, self.output)

    def row(self, period, basis='monthly', company='c'):
        return next(r for r in self.f.read('cash_flow_statement.csv') if (r['company_id'], r['period_end'], r['basis']) == (company, period, basis))

    def test_known_cash_rollforward_funding_expenses_reversals_and_drafts(self):
        self.build()
        jan = self.row('2024-01-31')
        self.assertEqual(tuple(jan[k] for k in ['beginning_cash', 'operating_inflows', 'operating_outflows', 'operating_net', 'financing_net', 'ending_cash']), ('0.00', '0.00', '20.00', '-20.00', '100.00', '80.00'))
        feb = self.row('2024-02-29')
        self.assertEqual((feb['beginning_cash'], feb['net_change'], feb['ending_cash']), ('80.00', '0.00', '80.00'))
        march = self.row('2024-03-31', 'fiscal_ytd')
        self.assertEqual((march['operating_net'], march['financing_net'], march['ending_cash']), ('-15.00', '100.00', '85.00'))
        self.assertEqual(self.row('2024-01-31', company='other')['ending_cash'], '7.00')
        self.assertFalse(any(r['journal_entry_id'] == 'draft' for r in self.f.read('cash_flow_details.csv')))

    def test_internal_transfer_and_noncash_depreciation_are_excluded(self):
        self.s.add_account('bank2', 'asset', 'current_asset')
        self.s.add_account('accum', 'asset', 'fixed_asset')
        self.f.entry('transfer', '2024-01-10', 'bank2', 'cash', '10', number=3)
        self.f.entry('depreciation', '2024-01-31', 'expense', 'accum', '3', number=4)
        self.s.mapping()
        mapping = json.loads(self.s.mapping_path.read_text())
        mapping['accounts']['bank2']['cash_flow_activity'] = 'cash'
        # Build statements using this explicit multi-bank policy.
        self.f.build()
        from accounting.statements import build_statements
        self.s.mapping_path.write_text(json.dumps(mapping))
        build_statements(self.f.snapshot, self.output, self.s.mapping_path)
        build_cash_flow(self.f.snapshot, self.output)
        validate_cash_flow(self.f.snapshot, self.output)
        excluded = {r['journal_entry_id']: r['reason'] for r in self.f.read('cash_flow_exclusions.csv')}
        self.assertEqual(excluded['transfer'], 'internal_transfer')
        self.assertEqual(excluded['depreciation'], 'noncash')
        self.assertEqual(self.row('2024-01-31')['ending_cash'], '80.00')
        self.assertEqual(self.row('2024-01-31')['operating_outflows'], '20.00')

    def test_investing_loan_equity_draw_and_simple_mixed_allocation(self):
        self.s.add_account('equipment', 'asset', 'fixed_asset')
        self.s.add_account('draw', 'equity', 'equity')
        self.s.add_account('loan', 'liability', 'non_current_liability')
        self.f.entry('equipment', '2024-01-11', 'equipment', 'cash', '30', number=3)
        self.f.entry('loan', '2024-01-12', 'cash', 'loan', '50', number=4)
        self.f.entry('draw', '2024-01-13', 'draw', 'cash', '7', number=5)
        # One-sided noncash legs: a cash payment for equipment and expense.
        self.f.tables['journal_entries'].append({'id': 'mixed', 'company_id': 'c', 'entry_date': '2024-01-14', 'entry_number': '6', 'posted': 'True'})
        for ident, account, debit, credit in [('m1', 'equipment', '10', '0'), ('m2', 'expense', '5', '0'), ('m3', 'cash', '0', '15')]:
            self.f.tables['journal_entry_lines'].append({'id': ident, 'journal_entry_id': 'mixed', 'account_id': 'c-' + account, 'description': ident, 'debit': debit, 'credit': credit})
        self.build()
        jan = self.row('2024-01-31')
        self.assertEqual((jan['operating_net'], jan['investing_net'], jan['financing_net'], jan['ending_cash']), ('-25.00', '-40.00', '143.00', '78.00'))

    def test_ambiguous_mixed_activity_is_blocked(self):
        self.s.add_account('loan', 'liability', 'non_current_liability')
        self.f.entry('fee-loan', '2024-01-11', 'cash', 'loan', '50', number=3)
        line = next(l for l in self.f.tables['journal_entry_lines'] if l['id'] == 'fee-loan-debit')
        line['debit'] = '45'
        self.f.tables['journal_entry_lines'].append({'id': 'fee', 'journal_entry_id': 'fee-loan', 'account_id': 'c-expense', 'description': 'fee', 'debit': '5', 'credit': '0'})
        self.s.build()
        with self.assertRaisesRegex(ValidationError, 'Ambiguous mixed-activity'):
            build_cash_flow(self.f.snapshot, self.output)
        self.assertEqual(self.f.read('cash_flow_summary.json')['status'], 'failed')

    def test_missing_role_or_no_cash_account_blocks_generation(self):
        self.build()
        mapping = self.f.read('statement_mapping.json')
        mapping['accounts']['unused'].pop('cash_flow_activity')
        write_json(self.output / 'statement_mapping.json', mapping)
        with self.assertRaisesRegex(ValidationError, 'classification'):
            build_cash_flow(self.f.snapshot, self.output)
        mapping['accounts']['unused']['cash_flow_activity'] = 'operating'
        mapping['accounts']['cash']['cash_flow_activity'] = 'operating'
        write_json(self.output / 'statement_mapping.json', mapping)
        with self.assertRaisesRegex(ValidationError, 'No cash accounts'):
            build_cash_flow(self.f.snapshot, self.output)

    def test_noncalendar_fiscal_year_and_year_start_cash(self):
        self.f.tables['companies'][0]['financial_year_end_month'] = '2'
        self.build()
        march = self.row('2024-03-31', 'fiscal_ytd')
        self.assertEqual((march['period_start'], march['beginning_cash'], march['operating_net'], march['financing_net'], march['ending_cash']), ('2024-03-01', '80.00', '5.00', '0.00', '85.00'))

    def test_wrong_amount_fails_independent_source_validation(self):
        self.build()
        rows = self.f.read('cash_flow_statement.csv')
        rows[0]['ending_cash'] = '999.00'
        write_csv(self.output / 'cash_flow_statement.csv', FIELDS, rows)
        receipt(self.f.snapshot, self.output, 'cash_flow', 'cash_flow_statement.csv')
        with self.assertRaisesRegex(ValidationError, 'validation failures'):
            validate_cash_flow(self.f.snapshot, self.output)
        self.assertGreater(self.f.read('cash_flow_validation_summary.json')['failed_check_count'], 0)

    def test_missing_source_evidence_or_period_cannot_pass(self):
        self.build()
        rows = self.f.read('cash_flow_details.csv')
        rows.pop()
        write_csv(self.output / 'cash_flow_details.csv', DETAIL_FIELDS, rows)
        receipt(self.f.snapshot, self.output, 'cash_flow_details', 'cash_flow_details.csv')
        with self.assertRaisesRegex(ValidationError, 'coverage'):
            validate_cash_flow(self.f.snapshot, self.output)
        self.assertEqual(self.f.read('cash_flow_validation_summary.json')['status'], 'failed')
        self.build()
        rows = self.f.read('cash_flow_statement.csv')
        rows.pop()
        write_csv(self.output / 'cash_flow_statement.csv', FIELDS, rows)
        receipt(self.f.snapshot, self.output, 'cash_flow', 'cash_flow_statement.csv')
        with self.assertRaisesRegex(ValidationError, 'coverage'):
            validate_cash_flow(self.f.snapshot, self.output)

    def test_cash_flow_failures_block_publication_and_preserve_latest(self):
        self.r.build()
        final = Path(publish_results(self.f.snapshot, self.output, self.r.published))
        latest = (self.r.published / 'latest.json').read_bytes()
        write_json(self.output / 'cash_flow_validation_summary.json', {'status': 'failed'})
        render_html(self.f.snapshot, self.output)
        with self.assertRaisesRegex(ValidationError, 'independently verified source'):
            publish_results(self.f.snapshot, self.output, self.r.published)
        self.assertEqual(latest, (self.r.published / 'latest.json').read_bytes())
        self.assertTrue(final.is_file())

    def test_repeatability(self):
        self.build()
        before = (self.output / 'cash_flow_statement.csv').read_bytes()
        build_cash_flow(self.f.snapshot, self.output)
        validate_cash_flow(self.f.snapshot, self.output)
        self.assertEqual(before, (self.output / 'cash_flow_statement.csv').read_bytes())

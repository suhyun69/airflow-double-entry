import json
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import test_statements as fixtures
from accounting.pipeline import ValidationError, profile, write_json
from accounting.ledger import reconcile_trial_balance
from accounting.statements import validate_statements
from accounting.cash_flow import build_cash_flow, validate_cash_flow
from accounting.reporting import ARTIFACTS, publish_results, render_html, sha


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = 0
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.scripts += 1
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href')

    def handle_data(self, data):
        self.text.append(data)


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.s = fixtures.StatementTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.f = self.s.f
        self.output = self.f.output
        self.published = self.f.snapshot.parent / 'published'

    def build(self, mismatch=False):
        if mismatch:
            self.f.tables['trial_balances'][0]['debit_balance'] = '999'
        self.s.build()
        profile(self.f.snapshot, self.output)
        try:
            reconcile_trial_balance(self.f.snapshot, self.output)
        except ValidationError:
            if not mismatch:
                raise
        validate_statements(self.f.snapshot, self.output)
        build_cash_flow(self.f.snapshot, self.output)
        validate_cash_flow(self.f.snapshot, self.output)
        return render_html(self.f.snapshot, self.output)

    def test_html_contains_statements_statuses_downloads_and_escaped_source(self):
        self.f.tables['companies'][0]['name'] = '<script>alert("x")</script>'
        self.f.tables['companies'][1]['name'] = 'Other & Company'
        self.build(mismatch=True)
        parser = ReportParser()
        parser.feed((self.output / 'report.html').read_text())
        self.assertEqual(parser.scripts, 0)
        self.assertIn('&lt;script&gt;', (self.output / 'report.html').read_text())
        self.assertTrue(set(ARTIFACTS) <= set(parser.links))
        for link in parser.links:
            if not link.startswith('#'):
                self.assertTrue((self.output / link).is_file(), link)
        self.assertIn('최종 발행 차단', ''.join(parser.text))
        self.assertIn('손익계산서', ''.join(parser.text))
        self.assertIn('재무상태표', ''.join(parser.text))
        self.assertIn('현금흐름표 (직접법)', ''.join(parser.text))
        self.assertIn('현금흐름표 검증', ''.join(parser.text))

    def test_clean_data_publishes_complete_immutable_idempotent_bundle(self):
        self.build()
        report = Path(publish_results(self.f.snapshot, self.output, self.published))
        self.assertTrue(report.is_file())
        self.assertIn('최종 발행</span>', report.read_text())
        self.assertIn('검토용 초안</span>', (self.output / 'report.html').read_text())
        manifest = json.loads((report.parent / 'release_manifest.json').read_text())
        self.assertEqual(manifest['status'], 'published')
        self.assertEqual(set(manifest['files']), set(ARTIFACTS) | {'report.html', 'source_manifest.json'})
        for name, checksum in manifest['files'].items():
            self.assertEqual(sha(report.parent / name), checksum)
        before = {p.name: p.read_bytes() for p in report.parent.iterdir()}
        self.assertEqual(publish_results(self.f.snapshot, self.output, self.published), str(report))
        self.assertEqual(before, {p.name: p.read_bytes() for p in report.parent.iterdir()})
        latest = json.loads((self.published / 'latest.json').read_text())
        self.assertEqual(latest['report'], f'{report.parent.name}/report.html')

    def test_failed_reconciliation_creates_draft_only(self):
        self.build(mismatch=True)
        with self.assertRaisesRegex(ValidationError, 'publication blocked'):
            publish_results(self.f.snapshot, self.output, self.published)
        self.assertFalse((self.published / 'latest.json').exists())
        self.assertFalse(self.published.exists())
        self.assertTrue((self.output / 'report.html').exists())
        self.assertEqual(self.f.read('publication_summary.json')['status'], 'blocked')

    def test_forged_passed_summary_does_not_bypass_source_recheck(self):
        self.build(mismatch=True)
        summary = self.f.read('reconciliation_summary.json')
        summary.update(status='passed', difference_count=0, source_error_count=0)
        write_json(self.output / 'reconciliation_summary.json', summary)
        render_html(self.f.snapshot, self.output)
        with self.assertRaisesRegex(ValidationError, 'Reconciliation failed'):
            publish_results(self.f.snapshot, self.output, self.published)
        self.assertFalse((self.published / 'latest.json').exists())

    def test_failed_attempt_preserves_previous_release_and_latest(self):
        self.build()
        final = Path(publish_results(self.f.snapshot, self.output, self.published))
        latest = (self.published / 'latest.json').read_bytes()
        final_before = final.read_bytes()
        write_json(self.output / 'statement_validation_summary.json', {'status': 'failed'})
        render_html(self.f.snapshot, self.output)
        with self.assertRaises(ValidationError):
            publish_results(self.f.snapshot, self.output, self.published)
        self.assertEqual(latest, (self.published / 'latest.json').read_bytes())
        self.assertEqual(final_before, final.read_bytes())

    def test_artifact_change_after_render_blocks_publication(self):
        self.build()
        with (self.output / 'income_statement.csv').open('a') as f:
            f.write('changed')
        with self.assertRaisesRegex(ValidationError, 'changed after HTML'):
            publish_results(self.f.snapshot, self.output, self.published)
        self.assertFalse((self.published / 'latest.json').exists())

    def test_html_change_and_missing_artifact_block_publication(self):
        self.build()
        with (self.output / 'report.html').open('a') as f:
            f.write('<p>changed</p>')
        with self.assertRaisesRegex(ValidationError, 'stale or changed'):
            publish_results(self.f.snapshot, self.output, self.published)
        (self.output / 'profile.json').unlink()
        render_html(self.f.snapshot, self.output)
        with self.assertRaisesRegex(ValidationError, 'artifacts are missing'):
            publish_results(self.f.snapshot, self.output, self.published)

    def test_missing_or_failed_checks_still_render_incomplete_draft(self):
        self.build()
        (self.output / 'statement_validation_summary.json').unlink()
        (self.output / 'balance_sheet_summary.json').unlink()
        render_html(self.f.snapshot, self.output)
        html = (self.output / 'report.html').read_text()
        self.assertIn('실패 / 미완료', html)
        self.assertIn('최종 발행 차단', html)
        with self.assertRaisesRegex(ValidationError, 'rendering errors'):
            publish_results(self.f.snapshot, self.output, self.published)

    def test_interrupted_staging_cannot_expose_partial_release(self):
        self.build()
        with patch('accounting.reporting.shutil.copytree', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValidationError, 'disk full'):
                publish_results(self.f.snapshot, self.output, self.published)
        self.assertFalse((self.published / 'latest.json').exists())
        self.assertEqual(list(self.published.iterdir()), [])

    def test_corrupted_existing_release_is_never_overwritten(self):
        self.build()
        final = Path(publish_results(self.f.snapshot, self.output, self.published))
        latest = (self.published / 'latest.json').read_bytes()
        final.write_text('corrupted')
        with self.assertRaisesRegex(ValidationError, 'corrupted'):
            publish_results(self.f.snapshot, self.output, self.published)
        self.assertEqual(final.read_text(), 'corrupted')
        self.assertEqual(latest, (self.published / 'latest.json').read_bytes())

"""Immutable as-of month snapshots with verifiable raw-source lineage."""
import hashlib
import json
import re
import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from accounting.ledger import month_end, write_csv
from accounting.pipeline import ValidationError, load, write_json


class NoDataPeriod(ValidationError):
    pass


def execution_period(conf, run_type, interval_start):
    kind = str(getattr(run_type, 'value', run_type)).lower().split('.')[-1]
    if conf.get('period') is not None:
        cutoff(conf['period'])
        return conf['period']
    if kind in {'scheduled', 'backfill', 'backfill_job'}:
        if interval_start is None:
            raise ValidationError('Scheduled/backfill runs require a data interval')
        return interval_start.astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m')
    if kind != 'manual':
        raise ValidationError(f'Unsupported run type: {kind}')
    return None


def cutoff(period):
    if not isinstance(period, str) or not re.fullmatch(r'\d{4}-\d{2}', period):
        raise ValidationError('period must be YYYY-MM')
    try:
        return month_end(date.fromisoformat(period + '-01')).isoformat()
    except ValueError as exc:
        raise ValidationError('Invalid accounting month') from exc


def select(tables, end):
    entries = [r for r in tables['journal_entries'] if date.fromisoformat(r['entry_date']).isoformat() <= end]
    ids = {r['id'] for r in entries}
    return {**tables, 'journal_entries': entries,
            'journal_entry_lines': [r for r in tables['journal_entry_lines'] if r['journal_entry_id'] in ids],
            'trial_balances': [r for r in tables['trial_balances'] if date.fromisoformat(r['period_end']).isoformat() <= end]}


def create_period_snapshot(source, root, period):
    from accounting.runs import locked
    source, root = Path(source).resolve(), Path(root).resolve()
    end = cutoff(period)
    tables, columns = load(source)
    original = json.loads((source / 'manifest.json').read_text())
    if 'scope' in original:
        raise ValidationError('Scope must be created directly from an unfiltered source snapshot')
    try:
        months = [date.fromisoformat(r['entry_date']).strftime('%Y-%m') for r in tables['journal_entries']]
        for row in tables['trial_balances']:
            date.fromisoformat(row['period_end'])
    except ValueError as exc:
        raise ValidationError('Invalid source date; cannot safely assign a month') from exc
    if not months or period < min(months) or period > max(months):
        raise NoDataPeriod(f'No source coverage for {period}; available {min(months) if months else None}..{max(months) if months else None}')
    if not any(r['period_end'] == end for r in tables['trial_balances']):
        raise ValidationError(f'Missing source trial balance for {period}')
    selected = select(tables, end)
    parent_sha = hashlib.sha256((source / 'manifest.json').read_bytes()).hexdigest()
    target = root / 'scoped' / 'v2' / parent_sha / period
    with locked(root / 'scoped' / '.lock'):
        if (target / 'manifest.json').exists():
            load(target)
            return str(target)
        target.mkdir(parents=True, exist_ok=True)
        manifest = {**{k: v for k, v in original.items() if k != 'files'},
                    'scope': {'period': period, 'as_of': end, 'source_path': os.path.relpath(source, target), 'source_manifest_sha256': parent_sha}, 'files': {}}
        for table, rows in selected.items():
            path = target / f'{table}.csv'
            write_csv(path, columns[table], rows)
            manifest['files'][table] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                        'source_sha256': original['files'][table]['sha256']}
        write_json(target / 'manifest.json', manifest)
    return str(target)


def verify_scope(snapshot, manifest, tables, columns):
    scope = manifest['scope']
    if scope['as_of'] != cutoff(scope['period']):
        raise ValidationError('Invalid snapshot cutoff')
    source = Path(scope['source_path'])
    if not source.is_absolute():
        source = (Path(snapshot) / source).resolve()
    if hashlib.sha256((source / 'manifest.json').read_bytes()).hexdigest() != scope['source_manifest_sha256']:
        raise ValidationError('Scoped source lineage changed')
    original_manifest = json.loads((source / 'manifest.json').read_text())
    if 'scope' in original_manifest:
        raise ValidationError('Nested period snapshots are not supported')
    original, original_columns = load(source)
    if columns != original_columns or tables != select(original, scope['as_of']):
        raise ValidationError('Scoped snapshot does not match the raw source cutoff')

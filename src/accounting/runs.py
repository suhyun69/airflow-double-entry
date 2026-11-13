"""Concurrent-safe execution records, attempts and an offline history dashboard."""
import hashlib
import json
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl

from accounting.pipeline import ValidationError, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class RunStore:
    def __init__(self, root, run_id):
        self.root = Path(root).resolve()
        self.run_id = run_id
        self.key = hashlib.sha256(run_id.encode()).hexdigest()
        self.path = self.root / 'runs' / f'{self.key}.json'

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {'run_id': self.run_id, 'tasks': []}

    def update(self, fn):
        with locked(self.root / 'runs' / '.lock'):
            record = self.read()
            fn(record)
            write_json(self.path, record)
            self._dashboard()
        return record

    def start(self, **metadata):
        def apply(record):
            if record.get('generation'):
                record.setdefault('generations', []).append({k: record.get(k) for k in
                    ('generation', 'period', 'mode', 'state', 'source', 'snapshot', 'output', 'publication', 'scope_error')})
            record.update(**metadata, state='running', updated_at=now(), generation=now(), no_data=None, failures=[], publication={}, ended_at=None,
                          output=None, output_relative=None, source=None, snapshot=None, publication_relative=None, scope_error=None)
            record.setdefault('started_at', now())
        return self.update(apply)

    def configure(self, **metadata):
        if metadata.get('output'):
            try:
                metadata['output_relative'] = str(Path(metadata['output']).resolve().relative_to(self.root))
            except ValueError:
                pass
        return self.update(lambda r: r.update(**metadata, updated_at=now()))

    @contextmanager
    def task(self, name, attempt=1, log_url=None):
        token = now()
        def start(r):
            r.setdefault('tasks', []).append({'task': name, 'attempt': attempt, 'started_at': token,
                                              'state': 'running', 'log_url': log_url, 'generation': r.get('generation')})
        self.update(start)
        try:
            yield
        except Exception as exc:
            state = 'skipped' if type(exc).__name__ in {'AirflowSkipException', 'NoDataPeriod'} else 'failed'
            self._task_end(token, name, state, error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
            raise
        else:
            self._task_end(token, name, 'success')

    def _task_end(self, token, name, state, **details):
        def apply(r):
            item = next(t for t in reversed(r['tasks']) if t['started_at'] == token and t['task'] == name)
            item.update(state=state, ended_at=now(), **details)
            r['updated_at'] = now()
        self.update(apply)

    def finish(self, require_publication=True):
        def apply(record):
            latest = {t['task']: t for t in record.get('tasks', []) if t.get('generation') == record.get('generation')}
            failures = [t for t in latest.values() if t['state'] in {'failed', 'running'}]
            output = str(self.root / record['output_relative']) if record.get('output_relative') else record.get('output')
            pub = Path(output) / 'publication_summary.json' if output else None
            publication = json.loads(pub.read_text()) if pub and pub.exists() else {}
            if publication.get('path'):
                try:
                    record['publication_relative'] = str(Path(publication['path']).relative_to(self.root))
                except ValueError:
                    pass
            if failures:
                state = 'failed'
            elif record.get('no_data'):
                state = 'no_data'
            elif require_publication and publication.get('status') != 'published':
                state = 'failed'
                failures.append({'task': 'publish_results', 'error': publication.get('reason', 'Publication incomplete or upstream tasks did not finish')})
            else:
                state = 'success'
            duration = (datetime.now(timezone.utc) - datetime.fromisoformat(record.get('generation', now()))).total_seconds()
            record.update(state=state, ended_at=now(), duration_seconds=duration, failures=[{k: t.get(k) for k in ('task', 'attempt', 'error_type', 'error', 'log_url')} for t in failures], publication=publication)
        record = self.update(apply)
        if record['state'] == 'failed':
            raise ValidationError('; '.join(f"{f['task']}: {f['error']}" for f in record['failures']))
        return record

    def _dashboard(self):
        rows = []
        for path in (self.root / 'runs').glob('*.json'):
            record = json.loads(path.read_text())
            latest = {t['task']: t for t in record.get('tasks', []) if t.get('generation') == record.get('generation')}
            failures = record.get('failures') or [t for t in latest.values() if t['state'] == 'failed']
            reason = '; '.join(f"{f.get('task')}: {f.get('error')}" for f in failures) or record.get('no_data') or ''
            output = str(self.root / record['output_relative']) if record.get('output_relative') else record.get('output')
            report_link = ''
            if output and (Path(output) / 'report.html').exists():
                try:
                    relative = (Path(output) / 'report.html').relative_to(self.root)
                    report_link = f'<a href="../{escape(str(relative), quote=True)}">보고서</a>'
                except ValueError:
                    pass
            if record.get('state') == 'success' and record.get('publication_relative'):
                report_link += ' <a href="../' + escape(record['publication_relative'], quote=True) + '/report.html">최종 발행</a>'
            display = dict(record)
            if display.get('started_at'):
                display['started_at'] = datetime.fromisoformat(display['started_at']).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')
            rows.append((record.get('started_at', ''), '<tr>' + ''.join(f'<td>{escape(str(display.get(k, "")))}</td>' for k in ('started_at', 'run_id', 'period', 'mode', 'state')) +
                         f'<td>{escape(reason)}</td><td><a href="{path.name}">시도·예외·로그</a> {report_link}</td></tr>'))
        html = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>회계 실행 이력</title><style>body{font:14px system-ui;background:#f4f6f6;color:#183142;padding:24px}table{border-collapse:collapse;background:white;width:100%}td,th{border-bottom:1px solid #ddd;padding:12px;text-align:left;overflow-wrap:anywhere}a{color:#155e7c}</style><h1>회계 파이프라인 실행 이력</h1><p>한국시간 표시 · JSON 시각은 UTC입니다. 실패 원인과 태스크별 재시도·예외 스택은 JSON에서 확인합니다. no_data는 원천 범위 밖이며 발행하지 않습니다.</p><table><thead><tr><th>시작</th><th>실행 ID</th><th>대상 월</th><th>모드</th><th>상태</th><th>원인</th><th>상세</th></tr></thead><tbody>'''
        html += ''.join(row for _, row in sorted(rows, reverse=True)) + '</tbody></table></html>'
        temporary = self.root / 'runs' / 'index.html.tmp'
        temporary.write_text(html, encoding='utf-8')
        temporary.replace(self.root / 'runs' / 'index.html')

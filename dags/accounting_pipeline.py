"""Monthly Seoul calendar intervals, explicit manual periods and persisted attempts."""
from datetime import timedelta
from functools import wraps
import pendulum
from airflow.sdk import dag, task
from airflow.timetables.interval import CronDataIntervalTimetable


def tracked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        import os
        from airflow.sdk import get_current_context
        from accounting.runs import RunStore
        ctx = get_current_context()
        store = RunStore(os.environ.get('ACCOUNTING_DATA_ROOT', '/opt/airflow/data'), ctx['run_id'])
        if fn.__name__ == 'prepare_run':
            kind = ctx['dag_run'].run_type
            store.start(kind=str(getattr(kind, 'value', kind)), mode=os.environ.get('ACCOUNTING_RECONCILIATION_MODE', 'strict'))
        ti = ctx['ti']
        with store.task(fn.__name__, getattr(ti, 'try_number', 1), str(getattr(ti, 'log_url', ''))):
            if args and isinstance(args[0], dict) and 'snapshot' in args[0]:
                from accounting.periods import execution_period
                expected = execution_period(ctx['dag_run'].conf or {}, ctx['dag_run'].run_type, ctx['data_interval_start'])
                if args[0].get('period') != expected:
                    from accounting.pipeline import ValidationError
                    raise ValidationError('Run scope mismatch: monthly execution cannot use a full-period context')
            return fn(*args, **kwargs)
    return wrapped


@dag(dag_id='accounting_quality_pipeline',
     schedule=CronDataIntervalTimetable('0 0 1 * *', timezone='Asia/Seoul'),
     start_date=pendulum.datetime(2024, 1, 1, tz='Asia/Seoul'), catchup=False,
     is_paused_upon_creation=False, max_active_runs=1, tags=['accounting', 'monthly', 'data-quality'])
def accounting_quality_pipeline():
    @task(retries=2, retry_delay=timedelta(seconds=30))
    @tracked
    def prepare_run():
        import os
        from pathlib import Path
        from airflow.sdk import get_current_context
        from airflow.exceptions import AirflowSkipException, AirflowFailException
        from accounting.pipeline import REVISION, ValidationError, collect
        from accounting.periods import NoDataPeriod, create_period_snapshot, execution_period
        from accounting.runs import RunStore
        ctx = get_current_context()
        root = Path(os.environ.get('ACCOUNTING_DATA_ROOT', '/opt/airflow/data'))
        store = RunStore(root, ctx['run_id'])
        conf = ctx['dag_run'].conf or {}
        mode = conf.get('reconciliation_mode', os.environ.get('ACCOUNTING_RECONCILIATION_MODE', 'strict'))
        try:
            period = execution_period(conf, ctx['dag_run'].run_type, ctx['data_interval_start'])
        except ValidationError as exc:
            raise AirflowFailException(str(exc)) from exc
        store.configure(period=period or 'full', mode=mode,
                        interval_start=ctx['data_interval_start'].isoformat(), interval_end=ctx['data_interval_end'].isoformat())
        try:
            if mode not in {'strict', 'adjusted'}:
                raise ValidationError('Unknown reconciliation mode')
            source = collect(root, conf.get('revision', os.environ.get('ACCOUNTING_SOURCE_REVISION', REVISION)))
            snapshot = create_period_snapshot(source, root, period) if period is not None else source
            output = root / 'reports' / Path(source).name / (period or 'full') / store.key
            store.configure(source=source, snapshot=snapshot, output=str(output.resolve()))
            return {'snapshot': snapshot, 'output': str(output.resolve()), 'reconciliation_mode': mode, 'period': period}
        except NoDataPeriod as exc:
            store.configure(no_data=str(exc))
            raise AirflowSkipException(str(exc)) from exc
        except ValidationError as exc:
            raise AirflowFailException(str(exc)) from exc

    @task
    @tracked
    def profile_source(context):
        from accounting.pipeline import profile
        profile(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def validate_journals(context):
        from accounting.pipeline import validate
        validate(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def build_general_ledger(context):
        from accounting.ledger import build_general_ledger as build
        build(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def build_trial_balance(context):
        from accounting.ledger import build_trial_balance as build
        build(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def reconcile_trial_balance(context):
        from accounting.ledger import reconcile_trial_balance as reconcile
        from accounting.pipeline import ValidationError
        try:
            reconcile(context['snapshot'], context['output'])
        except ValidationError:
            if context['reconciliation_mode'] == 'strict':
                raise
        return context

    @task(retries=0)
    @tracked
    def build_adjusted_trial_balance(context):
        from accounting.corrections import build_adjusted_trial_balance as build
        if context['reconciliation_mode'] == 'adjusted':
            build(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def reconcile_adjusted_trial_balance(context):
        from accounting.corrections import reconcile_adjusted_trial_balance as reconcile
        if context['reconciliation_mode'] == 'adjusted':
            reconcile(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def build_statements(context):
        from accounting.statements import build_statements as build
        build(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def validate_statements(context):
        from accounting.statements import validate_statements as check
        return check(context['snapshot'], context['output'])

    @task(retries=0)
    @tracked
    def build_cash_flow(context):
        from accounting.cash_flow import build_cash_flow as build
        build(context['snapshot'], context['output'])
        return context

    @task(retries=0)
    @tracked
    def validate_cash_flow(context):
        from accounting.cash_flow import validate_cash_flow as check
        return check(context['snapshot'], context['output'])

    @task(retries=0, trigger_rule='all_done')
    @tracked
    def render_html(context):
        if context is None:
            import os
            from airflow.sdk import get_current_context
            from airflow.exceptions import AirflowSkipException, AirflowFailException
            from accounting.runs import RunStore
            record = RunStore(os.environ.get('ACCOUNTING_DATA_ROOT', '/opt/airflow/data'), get_current_context()['run_id']).read()
            if record.get('no_data'):
                raise AirflowSkipException(record['no_data'])
            raise AirflowFailException('Preparation failed; see prepare_run exception in execution history')
        from accounting.reporting import render_html as render
        render(context['snapshot'], context['output'], context['reconciliation_mode'])
        return context

    @task(retries=0)
    @tracked
    def publish_results(context):
        import os
        from pathlib import Path
        from accounting.reporting import publish_results as publish
        root = Path(os.environ.get('ACCOUNTING_DATA_ROOT', '/opt/airflow/data')) / 'published'
        return publish(context['snapshot'], context['output'], root, context['reconciliation_mode'])

    @task(retries=0, trigger_rule='all_done')
    def finalize_run():
        import os
        from airflow.sdk import get_current_context
        from accounting.runs import RunStore
        return RunStore(os.environ.get('ACCOUNTING_DATA_ROOT', '/opt/airflow/data'), get_current_context()['run_id']).finish()['state']

    context = prepare_run()
    profiled = profile_source(context)
    journals = validate_journals(profiled)
    ledger = build_general_ledger(journals)
    trial = build_trial_balance(ledger)
    reconciliation = reconcile_trial_balance(trial)
    adjusted = build_adjusted_trial_balance(reconciliation)
    adjusted_checks = reconcile_adjusted_trial_balance(adjusted)
    statements = build_statements(trial)
    statement_checks = validate_statements(statements)
    cash = build_cash_flow(statements)
    cash_checks = validate_cash_flow(cash)
    report = render_html(context)
    adjusted_checks >> report
    statement_checks >> report
    cash_checks >> report
    published = publish_results(report)
    finished = finalize_run()
    for step in [context, profiled, journals, ledger, trial, reconciliation, adjusted, adjusted_checks,
                 statements, statement_checks, cash, cash_checks, report, published]:
        step >> finished


accounting_quality_pipeline()

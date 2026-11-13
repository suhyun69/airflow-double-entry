"""Run inside Airflow container; defaults to a native backfill dry run."""
import argparse
import json
import subprocess
from datetime import date
import shlex

parser = argparse.ArgumentParser(description='Monthly accounting Airflow backfill')
parser.add_argument('--from-month', required=True)
parser.add_argument('--to-month', required=True)
parser.add_argument('--mode', choices=['strict', 'adjusted'], default='strict')
parser.add_argument('--reprocess', choices=['none', 'failed', 'completed'], default='none')
parser.add_argument('--reverse', action='store_true')
parser.add_argument('--execute', action='store_true', help='Create real backfill runs; default is dry run')
args = parser.parse_args()
from accounting.periods import cutoff
cutoff(args.from_month)
cutoff(args.to_month)
if args.from_month > args.to_month:
    parser.error('from-month must not be after to-month')
command = ['airflow', 'backfill', 'create', '--dag-id', 'accounting_quality_pipeline',
           '--from-date', args.from_month + '-01T00:00:00+09:00', '--to-date', args.to_month + '-01T00:00:00+09:00',
           '--reprocess-behavior', args.reprocess, '--max-active-runs', '1',
           '--dag-run-conf', json.dumps({'reconciliation_mode': args.mode})]
if args.reverse:
    command.append('--run-backwards')
if not args.execute:
    command.append('--dry-run')
print(shlex.join(command), flush=True)
subprocess.run(command, check=True)

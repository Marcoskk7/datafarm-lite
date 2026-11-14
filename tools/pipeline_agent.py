#!/usr/bin/env python3
"""Source-checkout entry: bind input, load general context, supervise a supplied agent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from datafarm.evolution.integration import binding, capture, context_for, feedback, source_for
from datafarm.instructions import write_context
from datafarm.orchestration.cost_budget import TaskBudget, budget_for_work
from datafarm.orchestration.driver import drive, prepare, save, user_context
from datafarm.orchestration.runtime import foreign_host, process_identity, supervise
from datafarm.orchestration.task_wait import wait_for_task
from datafarm.render.delivery import verify_delivery
from datafarm.task.review_identity import runtime_dir


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--episode', type=Path, required=True)
    prep.add_argument('--request-file', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--target-count', type=int)
    prep.add_argument('--stop-after', choices=['render', 'white'], default='render',
                      help='explicit delivery boundary; white does not certify rendering or training')
    prep.add_argument('--evolution-store', type=Path, help='use active evaluated capability from this store')
    prep.add_argument('--scope', type=Path, help='JSON exact selectors for task family/hardware/provider/run')
    prep.add_argument('--cost-policy', type=Path, default=ROOT / 'configs/task-cost-policy.json',
                      help='whole-task model policy; default advisory target $30, limit $50; excludes rendering/GPU')
    prep.add_argument('--reference', type=Path, action='append', default=[], help='additional real photo/device evidence; repeatable')
    ctx = sub.add_parser('context')
    ctx.add_argument('--role', default='orchestrator')
    ctx.add_argument('--work', type=Path)
    ctx.add_argument('--output', type=Path, required=True)
    v = sub.add_parser('verify'); v.add_argument('--work', type=Path, required=True)
    cost = sub.add_parser('cost-status'); cost.add_argument('--work', type=Path, required=True)
    wait = sub.add_parser('wait'); wait.add_argument('--work', type=Path, required=True)
    wait.add_argument('--timeout', type=float, default=1800)
    wait.add_argument('--interval', type=float, default=5)
    d = sub.add_parser('drive')
    d.add_argument('--work', type=Path, required=True)
    d.add_argument('--driver', type=Path, required=True)
    d.add_argument('--max-sessions', type=int, default=1)
    d.add_argument('--timeout', type=float, default=1800)
    d.add_argument('--evolution-driver', type=Path, help='optional coding driver for one bounded post-run improvement cycle')
    d.add_argument('--evolution-policy', type=Path, help='trusted evaluator policy for post-run improvement')
    d.add_argument('--evolution-timeout', type=float, default=600)
    sup = sub.add_parser('supervise', help='persistent continuation; launch via run ledger')
    sup.add_argument('--work', type=Path, required=True)
    sup.add_argument('--config', type=Path, required=True)
    sup.add_argument('--resume', action='store_true', help='clear an explicit user stop; retain counters/IDs')
    stop = sub.add_parser('stop', help='request stop without cancelling external jobs')
    stop.add_argument('--work', type=Path, required=True)
    stop.add_argument('--reason', default='user stop')
    status = sub.add_parser('runtime-status')
    status.add_argument('--work', type=Path, required=True)
    f = sub.add_parser('feedback')
    f.add_argument('--work', type=Path, required=True)
    f.add_argument('--file', type=Path, required=True, help='visible host event JSON with kind, statement, source and optional scope')
    args = p.parse_args(argv)
    try:
        if args.cmd in {'context', 'drive', 'verify', 'cost-status', 'wait', 'supervise'} and args.work:
            pinned = source_for(args.work, ROOT)
            if pinned != ROOT.resolve():
                result = subprocess.run([sys.executable, str(pinned / 'tools/pipeline_agent.py'), *(argv or sys.argv[1:])])
                return result.returncode
        if args.cmd == 'prepare': result = prepare(args)
        elif args.cmd == 'context':
            result = write_context(args.output, args.role, root=ROOT, user=user_context(args.work),
                                   experience=context_for(args.work) if args.work else None)['receipt']
        elif args.cmd == 'verify': result = verify_delivery(args.work)
        elif args.cmd == 'cost-status': result = TaskBudget(args.work).status()
        elif args.cmd == 'wait': result = wait_for_task(args.work, args.timeout, args.interval)
        elif args.cmd == 'supervise': result = supervise(args, drive)
        elif args.cmd == 'stop':
            save(runtime_dir(args.work) / 'stop.json', {'reason': args.reason, 'at': time.time()})
            result = {'status': 'stop-requested'}
        elif args.cmd == 'runtime-status':
            rt = runtime_dir(args.work)
            result = json.loads((rt / 'supervisor.json').read_text()) if (rt / 'supervisor.json').exists() else {'status': 'not-started'}
            # Liveness is only observable on the supervisor's own host; elsewhere it is unknown.
            remote = foreign_host({'host': result.get('supervisor_host')})
            result['supervisor_alive'] = None if remote else bool(result.get('supervisor_pid') and process_identity(result['supervisor_pid']) == result.get('supervisor_identity') and process_identity(result['supervisor_pid']))
            worker = result.get('worker') or {}
            result['worker_alive'] = None if remote else bool(worker.get('pid') and process_identity(worker['pid']) == worker.get('process_identity') and process_identity(worker['pid']))
        elif args.cmd == 'feedback': result = feedback(args.work, json.loads(args.file.read_text()))
        else:
            if bool(args.evolution_driver) != bool(args.evolution_policy):
                raise ValueError('post-run evolution requires both driver and evaluator policy')
            result = drive(args)
            if budget_for_work(args.work):
                result['cost'] = TaskBudget(args.work).status()
            capture(args.work, kind='task-result', statement='Task driver returned; inspect delivery or remaining work.',
                    ref=str(args.work / 'state.json'), details=result)
            if args.evolution_driver:
                from datafarm.evolution.controller import cycle
                from datafarm.evolution.store import Store
                capability = binding(args.work)
                if not capability:
                    raise ValueError('post-run evolution requires a bound task workspace')
                try:
                    result['evolution'] = cycle(Store(capability['store']), root=ROOT,
                        driver=json.loads(args.evolution_driver.read_text()), policy_path=args.evolution_policy,
                        timeout=args.evolution_timeout)
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    # A broken optional improvement policy must not erase a
                    # valid task result or silently claim evolution succeeded.
                    result['evolution'] = {'status': 'error', 'reason': str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') not in {'failed', 'timeout', 'needs-input', 'session-budget-exhausted', 'cost-budget-blocked', 'recovery-required', 'stopped', 'stop-failed', 'error'} else 75
    except (ValueError, OSError, KeyError, TypeError) as e:
        if getattr(args, 'work', None) and args.cmd in {'drive', 'verify'}:
            try:
                capture(args.work, kind='task-result', statement='Task validation or driver error: ' + str(e),
                        ref=str(args.work / 'state.json'), details={'status': 'error'})
            except (OSError, ValueError, KeyError, TypeError):
                pass  # preserve the original error when recording is unavailable
        print(f'pipeline_agent: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

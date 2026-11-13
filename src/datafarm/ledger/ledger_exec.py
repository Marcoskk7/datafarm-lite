"""为一个 pipeline 运行执行单条 worker 命令的分离式哨兵进程。

由 ``tools/datafarm_runs.py launch`` 在其自己的 session 中启动。它启动真正的命令，
在命令存活期间更新 heartbeat，转发 SIGTERM/SIGINT，并把
退出码记入 run ledger，因此意外退出总是可见的。

用法：python -m datafarm.ledger.ledger_exec RUN_DIR WORKER_ID HEARTBEAT_SECONDS -- CMD...
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

from datafarm.ledger.ledger import RunLedger


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[3] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    ledger = RunLedger(argv[0])
    worker_id = argv[1]
    interval = max(5, int(argv[2]))
    command = argv[4:]
    env = dict(os.environ)
    env["DATAFARM_LEDGER_RUN_DIR"] = str(ledger.directory)
    env["DATAFARM_LEDGER_WORKER"] = worker_id
    try:
        child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL)
    except OSError as exc:
        ledger.end_worker(worker_id, 127, reason=f"spawn failed: {exc}")
        return 127
    ledger.update_worker(worker_id, note=f"child_pid={child.pid}")
    ledger.record_event("worker_child_started", worker=worker_id, child_pid=child.pid)

    received: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        received.append(signum)
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    while True:
        ledger.heartbeat(worker_id)
        try:
            code = child.wait(timeout=interval)
            break
        except subprocess.TimeoutExpired:
            continue
    reason = None
    if code < 0:
        reason = f"killed by signal {-code}"
    elif received:
        reason = f"exited after forwarded signal {received[-1]}"
    ledger.end_worker(worker_id, code, reason=reason)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

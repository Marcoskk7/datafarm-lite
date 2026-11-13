#!/usr/bin/env python3
"""将 worker 租约已消失（worker 崩溃 / 超时）的运行中 ticket 重新入队。

运行中的 ticket 命名为 <ticket>@<host>-<pid>-<profile>.json。若 <host> 上没有持有该 pid 的存活租约，ticket 会
回到 pending/<ticket profile>/（已渲染的分块保留；worker 从第一个缺失的分块继续）。租约
来自 `${GPU_SCHEDULER} leases`；队列位于 <run>/queue（run 目录 = --run-dir 或环境变量 H3_RUN）。每次调用
只跑一遍：在 worker 可能已死亡时手动运行，或由已经在监管该部署的任何进程运行。

  python3 h3_requeue_orphans.py [--run-dir <run>]
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

import h3_paths

# <host>-<pid>-<profile>: pid 是 profile（以字母开头）之前的最后一个全数字字段
CLAIM_TAG = re.compile(r"(.+)-(\d+)-([A-Za-z][\w.-]*)$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    h3_paths.add_run_dir_arg(ap)
    args = ap.parse_args()
    run = h3_paths.run_dir(args.run_dir)
    result = subprocess.run([h3_paths.gpu_scheduler(), "leases"], capture_output=True, text=True)
    if result.returncode:  # 读不到的租约表无法说明哪些 worker 还活着
        raise SystemExit(f"scheduler leases failed: {result.stderr.strip()[-300:]}")
    live_pids = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].startswith("h3-"):
            live_pids.add((parts[2], parts[4]))  # (host, worker pid)
    for t in sorted((run / "queue/running").glob("*.json")):
        name, _, tag = t.stem.rpartition("@")
        m = CLAIM_TAG.match(tag)
        if not m:
            continue
        host, pid, profile = m.groups()
        if (host, pid) in live_pids:
            continue
        try:  # 回到 ticket 自己的 profile 队列
            profile = json.loads(t.read_text()).get("profile") or profile
        except ValueError:
            pass
        dest = run / "queue/pending" / profile / f"{name}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(t.read_text())
        t.unlink()
        print("requeued", dest.name, "from dead", host, profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""用于 dry run 的替身 judge（像真正的 verifier 一样，在 rollout sandbox 之外运行）。

读取 ``<run>/native/trace.json`` 并写出 ``<run>/verdict.json``：当确定性分数
低于专家质量时判定为成功。模拟器可读取的受保护路径会记录在 ``leaked_paths`` 中
（dry run 会单独检查它们）。
"""

import json
import os
import sys


def main():
    run = sys.argv[1]
    with open(os.path.join(run, "native", "trace.json"), encoding="utf-8") as handle:
        trace = json.load(handle)
    leaked = [p for p, v in trace.get("probe", {}).items() if v == "readable"]
    success = trace["score"] < trace["quality"]
    error = round(20 + 100 * trace["score"], 1)
    verdict = {"schema": "fake.verdict/1", "m1": success, "m2": success,
               "fold_max_xy_mm": [error, error], "leaked_paths": leaked, "family": trace["family"]}
    with open(os.path.join(run, "verdict.json"), "w", encoding="utf-8") as handle:
        json.dump(verdict, handle, indent=1)
    print(json.dumps({"M2": success}))
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())

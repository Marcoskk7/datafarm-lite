#!/usr/bin/env python3
"""运行 vendored openpi trainer，并显式进行 target-mesh checkpoint 恢复。

``--action-adapter`` 会安装某种 action 表示的配置扩展；其余所有参数都传给 trainer。
"""
import argparse
import os
from pathlib import Path
import runpy
import sys

# 以路径方式启动：替换 sys.path 中的该目录，其模块不得遮蔽 trainer 的顶层导入。
sys.path[0] = str(Path(__file__).resolve().parents[2])


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--action-adapter", choices=("none", "joint", "delta-eef"), default="none")
    args, trainer_argv = parser.parse_known_args(argv)
    sys.argv = [sys.argv[0], *trainer_argv]
    if args.action_adapter == "delta-eef":
        from datafarm.training.delta_eef import install
        install()
    if args.action_adapter == "joint":
        from datafarm.training.joint_actions import install
        install()
    from datafarm.training.checkpoint_restore import install
    namespace = runpy.run_path(str(Path(os.environ["DATAFARM_TRAINER_ROOT"]) / "scripts/train.py"))
    if args.action_adapter == "joint" and "ACTION_GROUPS" in namespace:
        from datafarm.training.joint_actions import action_groups
        namespace["ACTION_GROUPS"] = action_groups()
    trainer = namespace["main"]
    install(trainer.__globals__)
    trainer(namespace["_config"].cli())


if __name__ == "__main__":
    main()

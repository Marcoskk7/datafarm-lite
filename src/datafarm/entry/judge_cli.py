"""Judge 正式入口：生成、验证、冻结与执行任务 Judge，并为 adapter/1 写入新版本。

用法：``python -m datafarm.entry.judge_cli {build,verify,evaluate,attach} ...``。
本入口是组合根：把 adapters 层的 Codex ``structured_call`` 注入 Judge 作者/审查者角色。
CLI 不提供任何网关开关；需要回环网关时由部署或本地工具调用 Python API 注入。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from datafarm.adapters.agent_harness.codex import structured
from datafarm.cap.io import read_json, write_json
from datafarm.judge.compiler import JudgeCompiler, evaluate_bundle, verify_bundle
from datafarm.judge.integration import attach_adapter
from datafarm.judge.protocol import build_input, file_sha, project_episode
from datafarm.judge.provider import JudgeCodexProvider


def build_provider(config, *, gateway=None) -> JudgeCodexProvider:
    """绑定 Codex 结构化调用；传输源码摘要进入构建身份。"""
    return JudgeCodexProvider(structured.validate_config(config), structured.structured_call,
                              transport_fingerprint=file_sha(Path(structured.__file__)), gateway=gateway)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m datafarm.entry.judge_cli', description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    build = subs.add_parser('build')
    for flag in ('task', 'scene', 'observations', 'cases', 'provider-config', 'work'):
        build.add_argument('--' + flag, type=Path, required=True)
    build.add_argument('--dependencies', type=Path, help='Host mapping of dependency names to local files')
    build.add_argument('--max-candidates', type=int, default=3)
    for command in ('evaluate', 'verify', 'attach'):
        child = subs.add_parser(command)
        child.add_argument('--bundle', type=Path, required=True)
        child.add_argument('--freeze-sha256', required=True)
        if command == 'evaluate':
            child.add_argument('--episode', type=Path, required=True)
            child.add_argument('--output', type=Path, required=True)
        if command == 'attach':
            child.add_argument('--adapter', type=Path, required=True)
            child.add_argument('--episode-relative', required=True)
            child.add_argument('--output', type=Path, required=True)
            child.add_argument('--public-task-output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            inputs = build_input(read_json(args.task), read_json(args.scene), read_json(args.observations),
                                 read_json(args.cases))
            # 连同确切输入文件存入冻结依赖，防止只绑定模型看见的摘要而丢失上游原件。
            deps = {'input-' + k: getattr(args, k) for k in ('task', 'scene', 'observations', 'cases')}
            if args.dependencies:
                deps.update(read_json(args.dependencies))
            provider = build_provider(read_json(args.provider_config))
            result = JudgeCompiler(provider, max_candidates=args.max_candidates).build(inputs, args.work, deps)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'frozen' else 2
        if args.command == 'evaluate':
            # --episode 是录制器的 measured-scene-episode/1 payload；投影只裁字段，不改 state_trace 字段名。
            result = evaluate_bundle(args.bundle, project_episode(read_json(args.episode)), args.freeze_sha256)
            write_json(args.output, result)
            # error/aborted 也要持久化结构化结果，由 harness 区分计数，不能转换成 false。
            return 0
        contract = verify_bundle(args.bundle, args.freeze_sha256)
        if args.command == 'attach':
            if args.output.exists() or args.public_task_output.exists():
                raise ValueError('Use new adapter/public task paths; existing task versions are immutable')
            raw = attach_adapter(read_json(args.adapter), args.bundle, args.freeze_sha256, args.episode_relative)
            write_json(args.output, raw)
            write_json(args.public_task_output, contract['public_task'])
        else:
            print(json.dumps({'status': 'verified', 'task_id': contract['task_id'], 'freeze_id': contract['freeze_id']}))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'{type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
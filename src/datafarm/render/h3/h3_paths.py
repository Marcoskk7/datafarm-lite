#!/usr/bin/env python3
"""H3 control-hybrid 渲染工具的部署绑定（仅用标准库）。

harness 没有机器默认值：run directory、H3 解释器、model/repo 根目录和 scheduler 都在
工具需要它们的时刻在此解析，缺失或不存在即 fail closed。

  run directory   --run-dir <dir>  or  env H3_RUN            (仅数据：tasks/, recipes/, pkgs/, queue/, cache/,
                                                              screen/, reviews/, eval/realpool/；绝不含代码)
  bindings        env <KEY>, 否则 env DATAFARM_<KEY>, 否则 runtime bindings 文件中的显式条目
                  (DATAFARM_BINDINGS, 否则 <DATAFARM_WORKSPACE 或本检出目录>/bindings.json)；除 H3_PYTHON
                  回退到当前解释器外，没有内建默认值。

Keys: H3_PYTHON（含 torch/diffusers/VideoX-Fun 依赖的 python）、H3_MODELS（包含 MiniMax-H3/Ref2VA 与
MiniMax-H3-Fun-Controlnet-Union/）、H3_VIDEOX_FUN（已应用 vendor/videox_fun 的 VideoX-Fun 检出目录）、
H3_TURBO_LORA（Sol-H3 Turbo 4-step Ref2VA LoRA 文件）、GPU_SCHEDULER（scheduler 可执行文件）、H3_EVAL_DINO
（用于冻结 EVAL-v1 评分器的 DINOv2-base 目录）、H3_SAM2（sam2.1-hiera-large 目录，仅 OOD 工具使用）、
H3_HED_ANNOTATOR_REPO（打过补丁的 VideoX-Fun 的 soft-HED 工具所读取的 ControlNet annotator 源码）。

  python3 h3_paths.py get H3_MODELS        # 打印解析后的值，或退出码 2 并给出原因
  python3 h3_paths.py get H3_RUN          # run directory（env H3_RUN）
  python3 h3_paths.py show                 # 每个 key：值或无法解析的原因
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]  # src/datafarm/render/h3 -> 携带此 harness 的 datafarm 源码检出目录
EVAL_DIR = REPO / "evaluations" / "render-h3"
BINDING_KEYS = ("H3_PYTHON", "H3_MODELS", "H3_VIDEOX_FUN", "H3_TURBO_LORA", "GPU_SCHEDULER", "H3_EVAL_DINO", "H3_SAM2",
                "H3_HED_ANNOTATOR_REPO")
TOKEN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_RUN_DIR: Path | None = None


class MissingBinding(SystemExit):
    """必需的部署值缺失或不存在（以 SystemExit 抛出，使 CLI 干净退出）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def add_run_dir_arg(parser) -> None:
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="H3 control-hybrid run directory (default: env H3_RUN; required, no implicit default)")


def set_run_dir(path) -> Path:
    """为本进程固定 run directory（CLI 在做任何工作之前用 --run-dir 调用它）。"""
    global _RUN_DIR
    if path is None:
        return run_dir()
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise MissingBinding(f"--run-dir {p} is not an existing directory")
    _RUN_DIR = p
    os.environ["H3_RUN"] = str(p)  # 子工具（enqueue -> package builders）继承同一 run directory
    return p


def run_dir(arg=None) -> Path:
    if arg is not None:
        return set_run_dir(arg)
    if _RUN_DIR is not None:
        return _RUN_DIR
    value = os.environ.get("H3_RUN")
    if not value:
        raise MissingBinding("H3 control-hybrid run directory is not set: pass --run-dir <dir> or export H3_RUN")
    p = Path(value).expanduser().resolve()
    if not p.is_dir():
        raise MissingBinding(f"H3_RUN={value} is not an existing directory")
    return p


def _bindings_file() -> dict:
    """仅取 runtime bindings 文件的显式条目（绝不使用 datafarm.runtime 默认值）。"""
    datafarm_src()  # 在此导入，使 `--help` 和纯环境变量查找从不触碰 bindings 文件
    from datafarm.runtime import explicit_bindings
    try:
        return explicit_bindings()
    except ValueError as e:
        raise MissingBinding(str(e)) from e


def binding(key: str, *, required: bool = True, must_exist: bool = True) -> str | None:
    """`key` 的解析值；仅当 binding 缺失且 required=False 时才为 None（否则请用 require()）。"""
    if key not in BINDING_KEYS:
        raise MissingBinding(f"unknown H3 control-hybrid binding {key}; known: {', '.join(BINDING_KEYS)}")
    value = os.environ.get(key) or os.environ.get("DATAFARM_" + key) or _bindings_file().get(key)
    if not value:
        if required:
            raise MissingBinding(f"{key} is not set: export {key}=<path> (or DATAFARM_{key}, or add it to the runtime "
                                 f"bindings file); see docs/reference/rendering.md")
        return None
    if TOKEN.search(value):
        raise MissingBinding(f"{key}={value} still contains an unexpanded ${{...}} binding")
    if must_exist and not Path(value).exists():
        raise MissingBinding(f"{key}={value} does not exist")
    return value


def require(key: str, *, must_exist: bool = True) -> str:
    """必需的 binding：始终是非空字符串，否则抛出 MissingBinding（绝不返回 None）。"""
    return binding(key, required=True, must_exist=must_exist)


def python_exe() -> str:
    return binding("H3_PYTHON", required=False) or sys.executable


def models_root() -> Path:
    return Path(require("H3_MODELS"))


def videox_fun_repo() -> Path:
    return Path(require("H3_VIDEOX_FUN"))


def turbo_lora() -> Path:
    return Path(require("H3_TURBO_LORA"))


def gpu_scheduler() -> str:
    return require("GPU_SCHEDULER")


def eval_dino() -> str:
    return require("H3_EVAL_DINO")


def sam2_model() -> str:
    return require("H3_SAM2")


def eval_script() -> Path:
    return EVAL_DIR / "h3eval.py"


_MODEL_LAYOUT = {  # key -> (binding 根, 其内路径)
    "base_model": ("H3_MODELS", ("MiniMax-H3", "Ref2VA")),
    "control_checkpoint": ("H3_MODELS", ("MiniMax-H3-Fun-Controlnet-Union", "MiniMax-H3-Fun-Controlnet-Union.safetensors")),
    "control_config": ("H3_VIDEOX_FUN", ("config", "minimax_h3", "minimax_h3_control.yaml")),
    "videox_fun_repo": ("H3_VIDEOX_FUN", ()),
    "hed_tool": ("H3_VIDEOX_FUN", ("tools", "prepare_hed_control.py")),
    "hed_checkpoint": ("H3_VIDEOX_FUN", ("models", "annotators", "ControlNetHED.pth")),
}


def default_model_path(key: str) -> str:
    """recipe 的 `control` 段未显式写明时所用的某个 model/repo 文件。"""
    root, parts = _MODEL_LAYOUT[key]
    return str(Path(require(root), *parts))


def default_model_paths() -> dict:
    return {key: default_model_path(key) for key in _MODEL_LAYOUT}


def expand(value):
    """把 ${KEY} binding 占位符（configs/render/h3-*.json）替换为解析后的值；未知 key 直接失败。"""
    if isinstance(value, str):
        def sub(match):
            return python_exe() if match[1] == "H3_PYTHON" else require(match[1])
        out = TOKEN.sub(sub, value)
        if TOKEN.search(out):
            raise MissingBinding(f"nested binding placeholders are not supported: {value}")
        return out
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def datafarm_src() -> Path:
    src = REPO / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return src


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="resolve H3 control-hybrid deployment bindings")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("key", choices=(*BINDING_KEYS, "H3_RUN", "REPO", "EVAL"))
    sub.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "get":
        try:
            if a.key == "H3_RUN":
                print(run_dir())
            elif a.key == "REPO":
                print(REPO)
            elif a.key == "EVAL":
                print(eval_script())
            elif a.key == "H3_PYTHON":
                print(python_exe())
            else:
                print(require(a.key))
        except MissingBinding as e:
            print(e, file=sys.stderr)
            return 2
        return 0
    for key in ("H3_RUN", *BINDING_KEYS):
        try:
            print(f"{key}={run_dir() if key == 'H3_RUN' else python_exe() if key == 'H3_PYTHON' else require(key)}")
        except MissingBinding as e:
            print(f"{key} unresolved: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

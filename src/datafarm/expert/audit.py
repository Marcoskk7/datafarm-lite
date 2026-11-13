"""在 harness 消耗 rollout 之前，对 agent 编写的 expert 做静态预检审计。

expert 插件运行在模拟器进程内，因此 OS 隔离无法阻止它触碰内存中的模拟器对象。
否则"只消费观测并返回关节计划；不得访问模拟器、布料或文件"这些规则就只能靠人工审计。
本审计把其中可检查的部分机械化，并在每次 rollout 请求和每次提交时运行：

* 文件可解析，且所请求的类位于模块顶层（拼错的类名会浪费一次计入配额的 GPU rollout）；
* 每个 import 都在适配器的 allowlist 内（numpy/scipy 以及该任务已文档化的后端模块）；
* 没有动态代码或文件 I/O 内建（``open``、``exec``、``eval``、``__import__`` 等），
  没有内省逃逸口（``__subclasses__``、frame 对象等），也没有诸如
  ``np.load``/``np.save`` 的模块级文件 I/O。

审计通过是必要条件而非充分条件：它记录在每次尝试旁边，以便操作者区分
"运行前被拒绝"与"运行后失败"。
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Any, Iterable, Mapping

DEFAULT_FORBIDDEN_CALLS = ("open", "exec", "eval", "compile", "__import__", "globals", "locals",
                           "vars", "breakpoint", "input", "memoryview", "help", "exit", "quit")
DEFAULT_FORBIDDEN_ATTRIBUTES = ("__subclasses__", "__globals__", "__builtins__", "__code__",
                                "__closure__", "__loader__", "__spec__", "__import__", "__bases__",
                                "__mro__", "f_back", "f_globals", "f_locals", "f_code",
                                "gi_frame", "cr_frame", "ag_frame", "tb_frame", "_getframe")
DEFAULT_FORBIDDEN_MODULE_ATTRIBUTES = ("load", "save", "savez", "savez_compressed",
                                       "savetxt", "loadtxt", "genfromtxt", "fromfile", "tofile",
                                       "memmap", "fromregex", "DataSource", "loadmat", "savemat",
                                       "open_memmap")


@dataclasses.dataclass
class AuditReport:
    ok: bool
    violations: list[dict[str, Any]]
    imports: list[str]
    classes: list[str]
    bytes: int

    def summary(self) -> str:
        if self.ok:
            return "audit ok"
        return "; ".join(f"line {v['line']}: {v['rule']}: {v['detail']}" for v in self.violations[:8])

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _allowed(module: str, allowed: Iterable[str]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in allowed)


def audit_expert_source(source: str | bytes, class_name: str | None,
                        policy: Mapping[str, Any]) -> AuditReport:
    data = source.encode("utf-8") if isinstance(source, str) else source
    violations: list[dict[str, Any]] = []
    max_bytes = int(policy.get("max_bytes", 400_000))
    if len(data) > max_bytes:
        return AuditReport(False, [{"line": 0, "rule": "size",
                                    "detail": f"{len(data)} bytes > {max_bytes}"}], [], [], len(data))
    try:
        text = data.decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", 0) or 0
        return AuditReport(False, [{"line": line, "rule": "syntax", "detail": str(exc)[:200]}], [], [],
                           len(data))

    allowed = tuple(policy.get("allowed_imports", ()))
    forbidden_calls = set(policy.get("forbidden_calls", DEFAULT_FORBIDDEN_CALLS))
    forbidden_attrs = set(policy.get("forbidden_attributes", DEFAULT_FORBIDDEN_ATTRIBUTES))
    forbidden_module_attrs = set(policy.get("forbidden_module_attributes",
                                            DEFAULT_FORBIDDEN_MODULE_ATTRIBUTES))
    module_aliases: dict[str, str] = {}
    imports: list[str] = []

    def flag(node: ast.AST, rule: str, detail: str) -> None:
        violations.append({"line": getattr(node, "lineno", 0), "rule": rule, "detail": detail})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
                if not _allowed(alias.name, allowed):
                    flag(node, "import", f"module {alias.name!r} is not on the allowlist")
                module_aliases[(alias.asname or alias.name).split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                flag(node, "import", "relative imports are not allowed")
                continue
            module = node.module or ""
            imports.append(module)
            if not _allowed(module, allowed):
                flag(node, "import", f"module {module!r} is not on the allowlist")
            for alias in node.names:
                if alias.name == "*":
                    flag(node, "import", f"'from {module} import *' hides what is used")
                elif alias.name in forbidden_module_attrs or alias.name in forbidden_calls:
                    flag(node, "file-io", f"'{alias.name}' imported from {module!r}")
                else:
                    full = f"{module}.{alias.name}"
                    if _allowed(full, allowed) or _allowed(module, allowed):
                        module_aliases.setdefault(alias.asname or alias.name, full)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in forbidden_calls:
                flag(node, "builtin", f"call to {node.func.id}()")
            if node.func.id in ("getattr", "setattr", "delattr", "hasattr") and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and (
                        arg.value in forbidden_attrs or arg.value.startswith("__")):
                    flag(node, "introspection", f"{node.func.id}(..., {arg.value!r})")
                elif not isinstance(arg, ast.Constant):
                    pass  # 动态属性名在 expert 代码中很常见；不计为违规
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in ("__builtins__", "__loader__", "__spec__"):
                flag(node, "introspection", f"use of {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in forbidden_attrs:
                flag(node, "introspection", f"attribute {node.attr}")
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in module_aliases \
                    and node.attr in forbidden_module_attrs:
                flag(node, "file-io", f"{root.id}...{node.attr} reads or writes files")

    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    if class_name is not None and class_name not in classes:
        violations.append({"line": 0, "rule": "class",
                           "detail": f"class {class_name!r} is not defined at module top level "
                                     f"(found: {', '.join(classes) or 'none'})"})
    violations.sort(key=lambda v: (v["line"], v["rule"]))
    return AuditReport(not violations, violations, sorted(set(imports)), classes, len(data))

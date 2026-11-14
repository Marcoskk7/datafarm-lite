"""模型客户端的凭据提供者。

客户端从不读取环境变量或众所周知的路径来获取 key。由调用方注入一个 provider；
标准实现从调用方显式传入路径的 keys 文件中解析 ``KEY=VALUE`` 行（可选带 ``export``
前缀）。值绝不会出现在 ``repr``、异常、receipt 或日志中。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Callable


class CredentialError(RuntimeError):
    """凭据无法加载。消息绝不包含敏感值。"""


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    endpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key or any(ch.isspace() for ch in self.api_key):
            raise CredentialError("API key is empty or contains whitespace")

    def __repr__(self) -> str:
        return f"Credentials(api_key=<redacted>, endpoint={self.endpoint!r})"


CredentialProvider = Callable[[], Credentials]


def parse_keys_text(text: str) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 行；允许 ``#`` 注释、空行和 ``export``。"""

    values: dict[str, str] = {}
    for number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum():
            raise CredentialError(f"keys file line {number}: invalid variable name")
        try:
            parsed = shlex.split(raw.strip())
        except ValueError:
            raise CredentialError(f"keys file line {number}: unbalanced quoting") from None
        values[key] = parsed[0] if parsed else ""
    return values


class KeysFileCredentials:
    """从调用方提供的 keys 文件加载 API key（以及可选的 endpoint）。

    文件在首次使用时惰性读取，且仅缓存在内存中。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        key_var: str = "API_KEY",
        endpoint_var: str | None = "END_POINT",
    ) -> None:
        self.path = Path(path)
        self.key_var = key_var
        self.endpoint_var = endpoint_var
        self._cached: Credentials | None = None

    def __repr__(self) -> str:
        return f"KeysFileCredentials(path={str(self.path)!r}, key_var={self.key_var!r})"

    def __call__(self) -> Credentials:
        if self._cached is None:
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError as error:
                raise CredentialError(
                    f"cannot read keys file {self.path}: {type(error).__name__}"
                ) from None
            values = parse_keys_text(text)
            key = values.get(self.key_var)
            if not key:
                raise CredentialError(f"{self.key_var} is missing from {self.path}")
            endpoint = values.get(self.endpoint_var) if self.endpoint_var else None
            self._cached = Credentials(api_key=key, endpoint=endpoint or None)
        return self._cached


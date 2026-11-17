"""Logical IDs and lossless, portable storage identities (no runtime locators)."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from datafarm.kernel.schema import IDENTIFIER_PATTERN, canonical_json_bytes

IDENTITY_PROFILE = "datafarm.logical-identity/1"
_DOMAIN = re.compile(r"[a-z][a-z0-9-]{0,31}")


def logical_id(value: Any, *, label: str = "identifier") -> str:
    """Validate without rewriting case, punctuation or length."""
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must match the canonical logical ID format")
    return value


def require_identity_profile(record: Mapping[str, Any]) -> None:
    if record.get("identity_profile") != IDENTITY_PROFILE:
        raise ValueError(f"identity_profile must be {IDENTITY_PROFILE}")


def storage_key(domain: str, identity: Mapping[str, Any]) -> str:
    """Hash a domain-separated JSON object, never a delimiter-joined string."""
    if not isinstance(domain, str) or _DOMAIN.fullmatch(domain) is None:
        raise ValueError("invalid storage domain")
    if not isinstance(identity, Mapping) or not identity:
        raise ValueError("storage identity must be a nonempty JSON object")
    data = canonical_json_bytes({"domain": domain, "identity": dict(identity),
                                 "profile": IDENTITY_PROFILE})
    return f"{domain}-{hashlib.sha256(data).hexdigest()}"


def verify_storage_key(key: Any, domain: str, stored: Any, expected: Mapping[str, Any]) -> None:
    """Bind the key and the original structured identity; fail on any mismatch."""
    if not isinstance(stored, Mapping) or canonical_json_bytes(stored) != canonical_json_bytes(expected):
        raise ValueError("stored identity differs from the expected identity")
    if key != storage_key(domain, expected):
        raise ValueError("storage key does not match its full identity")


def write_once_json(path: Path, record: Mapping[str, Any]) -> None:
    """Atomically publish immutable bytes without replacing an existing record.

    Concurrent identical writers may reuse the bytes. Any different bytes, symlink
    or unsupported hard-link filesystem fails closed; there is no overwrite fallback.
    """
    data = canonical_json_bytes(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        published = False
        try:
            os.link(temporary, path)
            published = True
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise ValueError("existing immutable record differs; refusing overwrite")
    finally:
        os.unlink(temporary)
    if published:
        os.chmod(path, 0o444)

"""NVIDIA SimReady retrieval over the Hugging Face Hub HTTP API (standard library only).

The dataset ``nvidia/simready-assets`` stores one directory per asset. Each asset carries its own
packaging manifest (``package_id``, ``license``, hashes) and a bill of materials listing every
file with its size and sha256. Retrieval here:

1. pins the dataset revision to a commit sha, so every later URL is immutable;
2. lists asset directories (paginated tree API) and ranks them against query terms;
3. optionally probes each candidate's physics layer for articulation joints, so a search can
   require e.g. a prismatic drawer without downloading the asset;
4. downloads an asset: verifies the BOM digest against the packaging manifest, then every file
   against the BOM, and writes a fetch manifest that later stages bind by content.

The endpoint is configurable (``HF_ENDPOINT`` convention) because ``huggingface.co`` is not
reachable from every deployment; ``hf-mirror.com`` serves the same API and redirects.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from datafarm.kernel.io import atomic_write_json, sha256_file, utc_now

DEFAULT_ENDPOINT = "https://huggingface.co"
DATASET = "nvidia/simready-assets"
PACKAGING = "com.nvidia.simready.packaging.json"
BOM = ".metadata/com.nvidia.simready.packaging.bom.json"
ROOT_USDS = ".metadata/com.nvidia.simready.root_usds.json"
LICENSE = ".metadata/LICENSE.txt"
PHYSICS_LAYER = "simready_usd/payloads/Physics/physics.usda"
FETCH_MANIFEST = "simready-fetch.json"
FETCH_SCHEMA = "datafarm.simready-fetch/1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_JOINT = re.compile(r"def\s+Physics(Prismatic|Revolute|Fixed|Spherical)Joint\s+\"([^\"]+)\"")


class SimReadyError(RuntimeError):
    pass


def endpoint_from_env() -> str:
    return os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def tokens(text: str) -> list[str]:
    """Split asset names and queries into lowercase words: ``bigPlate_a01`` -> big, plate, a01."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def score(name: str, terms: Iterable[str]) -> float:
    """Word-level match score; 0 means no query term matched the asset name."""
    words = tokens(name)
    total = 0.0
    for term in terms:
        term = term.lower()
        if term in words:
            total += 2.0
        elif any(w.startswith(term) or term in w for w in words):
            total += 1.0
    return total


def parse_joints(physics_usda: str) -> list[dict[str, Any]]:
    """Joints declared in a SimReady physics layer, with axis and limits when authored."""
    joints = []
    matches = list(_JOINT.finditer(physics_usda))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(physics_usda)
        body = physics_usda[match.end():end]

        def attr(name: str) -> str | None:
            found = re.search(r"physics:" + name + r"\s*=\s*\"?([^\"\n]+)\"?", body)
            return found.group(1).strip() if found else None

        joint: dict[str, Any] = {"name": match.group(2), "type": match.group(1).lower()}
        if attr("axis"):
            joint["axis"] = attr("axis")
        for key in ("lowerLimit", "upperLimit"):
            value = attr(key)
            if value is not None:
                try:
                    joint[key] = float(value)
                except ValueError:
                    pass
        joints.append(joint)
    return joints


@dataclass
class Candidate:
    asset: str
    score: float
    joints: list[dict[str, Any]] | None = None
    license: str | None = None
    package_id: str | None = None
    collection: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


def parse_collection(physics_usda: str) -> str | None:
    """SimReady records the source kit, e.g. ``props_endeavor_kitchen``, in the layer metadata."""
    found = re.search(r"asset_dir\s*=\s*\"[^\"]*?/([^/\"]+)/[^/\"]+\"", physics_usda)
    return found.group(1) if found else None


@dataclass
class SimReadyClient:
    endpoint: str = field(default_factory=endpoint_from_env)
    dataset: str = DATASET
    revision: str = "main"
    timeout_s: float = 60.0
    retries: int = 3
    opener: Callable[..., Any] = urllib.request.urlopen

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        parsed = urllib.parse.urlsplit(self.endpoint)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.query:
            raise SimReadyError("endpoint must be a plain http(s) URL without credentials or query")

    # -- HTTP ---------------------------------------------------------------------------------
    def _rehost(self, url: str) -> str:
        """Pagination links point at huggingface.co; keep following the configured endpoint."""
        parsed, base = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.endpoint)
        return urllib.parse.urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, ""))

    def _open(self, url: str):
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self.opener(urllib.request.Request(url, headers={"User-Agent": "datafarm-lite"}),
                                   timeout=self.timeout_s)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 404):
                    raise SimReadyError(f"{exc.code} for {url}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(min(2 ** attempt, 8))
        raise SimReadyError(f"request failed after {self.retries} attempts: {url}: {last}")

    def _json(self, url: str) -> tuple[Any, str]:
        with self._open(url) as response:
            return json.loads(response.read()), response.headers.get("Link") or ""

    def _quote(self, relative: str) -> str:
        return urllib.parse.quote(relative, safe="/@")

    def file_url(self, asset: str, relative: str) -> str:
        return f"{self.endpoint}/datasets/{self.dataset}/resolve/{self.revision}/{self._quote(asset + '/' + relative)}"

    def read_bytes(self, asset: str, relative: str) -> bytes:
        with self._open(self.file_url(asset, relative)) as response:
            return response.read()

    def read_json(self, asset: str, relative: str) -> Any:
        return json.loads(self.read_bytes(asset, relative))

    # -- revision and index -------------------------------------------------------------------
    def pin(self) -> str:
        """Resolve ``revision`` to a commit sha so every later URL is immutable."""
        if _COMMIT.fullmatch(self.revision):
            return self.revision
        info, _ = self._json(f"{self.endpoint}/api/datasets/{self.dataset}/revision/{self.revision}")
        sha = info.get("sha") if isinstance(info, dict) else None
        if not isinstance(sha, str) or not _COMMIT.fullmatch(sha):
            raise SimReadyError(f"cannot resolve revision {self.revision!r}")
        self.revision = sha
        return sha

    def list_assets(self) -> list[str]:
        url = f"{self.endpoint}/api/datasets/{self.dataset}/tree/{self.revision}?recursive=false&limit=1000"
        names: list[str] = []
        while url:
            page, link = self._json(url)
            names.extend(item["path"] for item in page if item.get("type") == "directory")
            match = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
            url = self._rehost(match.group(1)) if match else ""
        return sorted(set(names))

    def probe(self, asset: str) -> Candidate:
        """Read packaging manifest, source collection and physics joints without downloading geometry."""
        packaging = self.read_json(asset, PACKAGING)
        try:
            layer = self.read_bytes(asset, PHYSICS_LAYER).decode("utf-8", "replace")
            joints, collection = parse_joints(layer), parse_collection(layer)
        except SimReadyError:
            joints, collection = None, None
        return Candidate(asset=asset, score=0.0, joints=joints, collection=collection,
                         license=packaging.get("license"), package_id=packaging.get("package_id"))

    def search(self, terms: list[str], *, context: list[str] = (), names: list[str] | None = None,
               limit: int = 10, require_joint: str | None = None, min_joints: int = 1,
               probe: bool = False, probe_limit: int = 40, workers: int = 8) -> list[Candidate]:
        """Rank asset directories against ``terms``.

        Names are matched first. With ``probe`` (implied by ``require_joint``) the top
        ``probe_limit`` names are probed in parallel and re-ranked by name, source collection and
        joint names; ``context`` terms (e.g. ``kitchen``) only score against collection and joints.
        """
        if not terms:
            raise SimReadyError("search needs at least one term")
        pool = names if names is not None else self.list_assets()
        ranked = sorted(((score(n, terms), n) for n in pool), key=lambda item: (-item[0], item[1]))
        ranked = [(s, n) for s, n in ranked if s > 0]
        if not (probe or require_joint):
            return [Candidate(asset=n, score=s) for s, n in ranked[:limit]]
        head = ranked[:probe_limit]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool_exec:
            probed = list(pool_exec.map(lambda item: self.probe(item[1]), head))
        results: list[Candidate] = []
        for (value, _), candidate in zip(head, probed):
            matching = [j for j in candidate.joints or [] if j["type"] == require_joint]
            if require_joint and len(matching) < min_joints:
                continue
            extra = " ".join([candidate.collection or ""] + [j["name"] for j in candidate.joints or []])
            candidate.score = value + 0.5 * score(extra, [*terms, *context])
            results.append(candidate)
        results.sort(key=lambda c: (-c.score, c.asset))
        return results[:limit]

    # -- download -----------------------------------------------------------------------------
    def fetch(self, asset: str, destination: Path, *, skip: Callable[[str], bool] | None = None,
              log: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
        """Download one asset into ``destination/<asset>`` and verify it against its own BOM.

        Files already present with the right digest are kept (resume). Returns the fetch manifest.
        """
        if not _ASSET.fullmatch(asset):
            raise SimReadyError(f"invalid asset name {asset!r}")
        commit = self.pin()
        root = Path(destination) / asset
        packaging_bytes = self.read_bytes(asset, PACKAGING)
        packaging = json.loads(packaging_bytes)
        bom_bytes = self.read_bytes(asset, BOM)
        declared = {m.get("name"): m.get("hash", {}).get("sha256") for m in packaging.get("metadata", [])}
        bom_digest = hashlib.sha256(bom_bytes).hexdigest()
        if declared.get(PurePosixPath(BOM).name) not in (None, bom_digest):
            raise SimReadyError(f"{asset}: BOM digest does not match the packaging manifest")
        bom = json.loads(bom_bytes)
        items = bom.get("items")
        if not isinstance(items, list) or not items:
            raise SimReadyError(f"{asset}: empty bill of materials")
        metadata = {PACKAGING: packaging_bytes, BOM: bom_bytes}
        for relative in (ROOT_USDS, LICENSE):
            metadata[relative] = self.read_bytes(asset, relative)
        for relative, data in metadata.items():
            _write(root / relative, data)
        files, skipped = [], []
        for item in items:
            relative, digest, size = item["relative_path"], item["hash"]["sha256"], int(item["size"])
            _safe_relative(relative)
            if skip and skip(relative):
                skipped.append(relative)
                continue
            target = root / relative
            if not (target.is_file() and target.stat().st_size == size and sha256_file(target) == digest):
                log(f"{asset}: {relative} ({size} B)")
                self._download(asset, relative, target, digest, size)
            files.append({"relative_path": relative, "sha256": digest, "size": size})
        roots = json.loads(metadata[ROOT_USDS]).get("entries") or []
        manifest = {
            "schema": FETCH_SCHEMA,
            "asset": asset,
            "dataset": self.dataset,
            "revision": commit,
            "source_uri": f"https://huggingface.co/datasets/{self.dataset}/tree/{commit}/{asset}",
            "package_id": packaging.get("package_id"),
            "license": packaging.get("license") or "unknown",
            "package_hash": packaging.get("package_hash", {}).get("sha256"),
            "content_hash": packaging.get("content_hash", {}).get("sha256"),
            "bom_sha256": bom_digest,
            "root_usds": roots,
            "retrieved_at": utc_now(),
            "files": files,
            "skipped": skipped,
            "metadata_files": {k: hashlib.sha256(v).hexdigest() for k, v in metadata.items()},
        }
        atomic_write_json(root / FETCH_MANIFEST, manifest)
        return manifest

    def _download(self, asset: str, relative: str, target: Path, digest: str, size: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(self.retries):
            handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            hasher, written = hashlib.sha256(), 0
            try:
                with os.fdopen(handle, "wb") as out, self._open(self.file_url(asset, relative)) as response:
                    for block in iter(lambda: response.read(1 << 20), b""):
                        out.write(block)
                        hasher.update(block)
                        written += len(block)
                if written == size and hasher.hexdigest() == digest:
                    os.replace(temporary, target)
                    return
            except (OSError, SimReadyError):
                if attempt == self.retries - 1:
                    raise
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        raise SimReadyError(f"{asset}/{relative}: downloaded bytes do not match the BOM digest")


def thumbnails_only_skip(relative: str) -> bool:
    """Default skip rule: texture preview thumbnails are not referenced by any USD layer."""
    parts = PurePosixPath(relative).parts
    return ".thumbs" in parts and parts[:2] != ("simready_usd", ".thumbs")


def verify_fetch(root: Path) -> dict[str, Any]:
    """Re-hash a fetched asset directory against its fetch manifest."""
    manifest = json.loads((Path(root) / FETCH_MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("schema") != FETCH_SCHEMA:
        raise SimReadyError("not a SimReady fetch manifest")
    for item in manifest["files"]:
        path = Path(root) / item["relative_path"]
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise SimReadyError(f"fetched file changed or missing: {item['relative_path']}")
    return manifest


def _safe_relative(relative: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SimReadyError(f"unsafe BOM path {relative!r}")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

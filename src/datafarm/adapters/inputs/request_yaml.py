"""Bounded YAML ingestion, immutable original bytes and explicit request profile."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from datafarm.kernel.artifacts import ArtifactRef
from datafarm.kernel.store import ArtifactStore
from datafarm.task.request_profile import REQUEST_SCHEMA, validate_request

MAX_BYTES = 1 << 20
MAX_DEPTH = 32
MAX_EVENTS = 10000


def parse_request(data: bytes, original_ref: ArtifactRef) -> dict[str, Any]:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError("request YAML must contain at most 1 MiB of UTF-8 bytes")
    from datafarm.kernel.io import sha256_bytes

    if (original_ref.kind != "task-request-source" or original_ref.media_type != "application/yaml"
            or original_ref.sha256 != sha256_bytes(data)):
        raise ValueError("original request reference does not bind these exact bytes")
    import yaml

    class RequestLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if not isinstance(key, str) or key in result:
                raise ValueError("YAML mapping keys must be unique strings; merge keys are unsupported")
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    RequestLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        source = data.decode("utf-8-sig")
        depth = count = documents = 0
        allowed_tags = {"tag:yaml.org,2002:" + name for name in ("str", "int", "float", "bool", "null", "seq", "map")}
        for event in yaml.parse(source):
            count += 1
            if count > MAX_EVENTS or getattr(event, "anchor", None) or isinstance(event, yaml.events.AliasEvent):
                raise ValueError("YAML aliases, anchors or oversized structures are unsupported")
            tag = getattr(event, "tag", None)
            if tag is not None and tag not in allowed_tags:
                raise ValueError("unsupported YAML tag")
            if isinstance(event, yaml.events.DocumentStartEvent):
                documents += 1
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise ValueError("request YAML nesting exceeds 32 levels")
            if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        if documents != 1:
            raise ValueError("request YAML must contain exactly one document")
        value = yaml.load(source, Loader=RequestLoader)
        if not isinstance(value, dict) or value.get("schema") != REQUEST_SCHEMA or "original_request_ref" in value:
            raise ValueError("unsupported YAML format or supplied original-request binding")
        return validate_request({**value, "original_request_ref": original_ref.to_dict()})
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("invalid request YAML") from exc


def load_request(path: str | Path, store: ArtifactStore) -> tuple[dict[str, Any], ArtifactRef]:
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    from datafarm.kernel.io import sha256_bytes

    ref = ArtifactRef(kind="task-request-source", sha256=sha256_bytes(data), media_type="application/yaml")
    request = parse_request(data, ref)
    # Invalid requests do not publish normalized objects or authorize side effects.
    actual = store.put_bytes(ref.kind, data, media_type=ref.media_type)
    if actual != ref:
        raise ValueError("request source binding changed")
    return request, store.put_json("task-request", request)

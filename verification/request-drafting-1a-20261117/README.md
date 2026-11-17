# 1a verification: one sentence → request.yaml against a real LLM

Date: 2026-11-17. Branch `feat/A-request-authoring`, base commit `f8efb788f6c4a87590e1d1f16908cdeedcc98493`
(exported with `git archive`, no working-tree changes of other agents involved). The adapter fix in this
directory's commit is on top of that base.

## Where it ran

- Node `lab30run`, new directory `/opt/data/private/isaaclab/request-authoring/a` (tar over ssh; the
  pre-existing `.../request-authoring/b`, `datafarm-lite`, `a-scene`, `PyRUA-Lean` were not touched).
- venv from `/home/isaac/.local/bin/python3.12` (uv-managed CPython 3.12.16), `pip install -e '.[dev,workflow]'`
  from `https://pypi.tuna.tsinghua.edu.cn/simple`. openai 2.54.0, pydantic 2.14.3.
- `PYTHONPATH=src:tests python -m pytest -q tests/test_request_schema.py tests/test_request_drafting.py`
  → 32 passed before the fix, 33 passed after.

## Endpoint (no secret values recorded)

Observed, by following processes and files on the node:

| Layer | What it is |
|---|---|
| Claude Code CLI | `/home/isaac/node/bin/claude` → `@anthropic-ai/claude-code` bun binary; no `~/.claude/settings.json`, no `ANTHROPIC_*` in env; transcript records model `claude-opus-5-5` |
| `127.0.0.1:4001` | `anthropic_openai_adapter.py`: Anthropic `/v1/messages` → OpenAI `/v1/chat/completions`, upstream `127.0.0.1:8899` |
| `127.0.0.1:8899` | `fyp_pi_bridge.py`: OpenAI-compatible bridge → `https://fyp-nus-gateway.tail5b2dc5.ts.net/v1` through the tailscale HTTP proxy `127.0.0.1:1054` |
| `127.0.0.1:4000` | litellm, model group `fyp-opus`, `api_base: http://127.0.0.1:8899/v1`, master key from `/home/isaac/fyp-bridge/litellm_config.yaml` |

Credential material found only in `/home/isaac/fyp-bridge/FYP-marcus20220122.env` (`OPENAI_BASE_URL`,
`OPENAI_API_KEY` starting `fyp_`; keys never printed). Nothing was written to a repo file, argv or log:
the run shell exported the litellm master key into `FYP_LLM_KEY` and `configs/llm.json` names only that
env var. `llm.json` here has no secrets.

Probes (status/model names only):

- `GET http://127.0.0.1:4000/v1/models` → 200, models `["fyp-opus"]`
- `POST /v1/chat/completions` plain → 200, content `ok`
- `POST /v1/chat/completions` with `response_format={"type":"json_schema",...}` → **400**
  `NUS upstream did not accept this request` (same on 4000 and directly on 8899)
- `POST /v1/chat/completions` with `response_format={"type":"json_object"}` → 200, but content is wrapped
  in a ```` ```json ```` fence

So: an OpenAI-compatible `/v1/chat/completions` endpoint does exist and was used, with
`response_format: json_object` because `json_schema` is rejected upstream. `tailscale status --json`
reported the gateway peer `offline, last seen 3d ago`; requests still succeeded through the local
tailscale proxy — routing was not investigated further (unknown below).

## Config used in the run directory

```json
{
  "provider": "openai",
  "model": "fyp-opus",
  "base_url": "http://127.0.0.1:4000/v1",
  "api_key_env": "FYP_LLM_KEY",
  "timeout_s": 120,
  "response_format": "json_object"
}
```

(`llm.json` in this directory.) The key was injected per shell:

```bash
export FYP_LLM_KEY=$(python3 -c 'import re;print(re.search(r"master_key:\s*(\S+)",open("/home/isaac/fyp-bridge/litellm_config.yaml").read()).group(1))')
```

## Runs

| Dir | Sentence | Result | episodes / cameras / views | tokens in/out | latency |
|---|---|---|---|---|---|
| `evidence/zh` | 把带帮的浅盘放进已经拉开的抽屉 | ok (2nd attempt) | 20 / head,left_wrist / head,third_person | 3393 / 301 | 5.8 s |
| `evidence/en1` | put the plate with a raised rim into the open drawer | ok | 20 / head,left_wrist / head,third_person | 3388 / 195 | 4.9 s |
| `evidence/en2` | same | ok | same | 3388 / 180 | 4.9 s |
| `evidence/third1` | 50 episodes, only the head camera: put the mug on the shelf | ok | 50 / head / head,third_person | 3393 / 177 | 5.0 s |
| `evidence/third2` | same | ok | same | 3393 / 177 | 5.0 s |

Commands (run from `/opt/data/private/isaaclab/request-authoring/a`):

```bash
python tools/request_draft.py "<sentence>" --config configs/llm.json --out runs/<dir>/request.yaml
```

On the node each `runs/<dir>/` kept `request.yaml`, `request.draft.json`, `agent-calls.jsonl`; they are
copied here under `evidence/` (`runs/` is gitignored in this repo). The sentence-given
fields win over defaults: the third sentence keeps `episodes: 50`, `cameras: [head]`, and derives
`render.views: [head, third_person]` from those cameras.

## Bug found and fixed

The first run attempt (remote `runs/zh`, local `evidence/zh`) **failed**: `agent-calls.jsonl` line 1 is `call-2fc40f7a164c4a58b8bd94f894cca002`,
`status: invalid_response`, `error_type: not_json`. The provider returned valid JSON inside a
```` ```json ```` fence, and `OpenAIStructuredLLM._parse` fed the fence to `json.loads`. Reproduced by
sending the exact messages+schema: content began `"```json\n{\n  \"task\": ..."` and `finish_reason: stop`.

Fix (`src/datafarm/adapters/llm/client.py`): `_unfence()` strips one leading ```` ``` ```` block before
`json.loads`; anything else still records `invalid_response/not_json`. Regression test
`test_adapter_accepts_json_wrapped_in_a_markdown_fence` in `tests/test_request_drafting.py`; doc line added
in `docs/reference/request-drafting.md`. Justification: `json_object` only promises "is JSON"; fence
wrapping is transport formatting, not a draft contract violation. No retries were added.

## Diffs against `examples/request/plate-drawer-airbot.yaml` (ignoring `request_id`)

Full diffs in `diffs/`.

- **en1/en2**: semantically equal to the example except `scene.setting: kitchen` and
  `scene.asset_sources: [simready, partnet_mobility]` are absent. The sentence names no place, and the
  extraction prompt never asks for `asset_sources`, so neither can come from 1a. Everything else matches:
  same task, three objects, same roles, `prismatic/open` drawer, `raised_rim` plate on `object.table`,
  episode/seed/camera/render/dataset values. Byte-level diff is mostly YAML block style, plus `count: 1`
  on each object.
- **zh**: same as en except `task: put the shallow plate into the open drawer` (a correct English
  rendering of 带帮的浅盘 and 已经拉开的抽屉 → `raised_rim` + `joint_state: open`) and the matching
  `request_id`. `request_id` is the kebab-case task slug.
- **third1/third2**: expected differences for the explicit fields (`episodes: 50`, `cameras: [head]`,
  `render.views: [head, third_person]`); scene is `table` + `shelf` (receptacle) + `mug` (task_object,
  supported by the table).

`count: 1` is not model noise in the YAML: `SceneObject.count` defaults to 1 and the CLI dumps with
`exclude_none`, not `exclude_defaults`, so it is always printed. It is schema-equal to the example's
omitted `count`.

## Stability

`en1` and `en2` are byte-identical `request.yaml`; `third1` and `third2` likewise. Output token counts
differ slightly between runs (195 vs 180) but the parsed request does not. `prompt_file_sha256`
(`93f8bb5a2281…`) matches the checkout's `prompts/request/draft.md`; `prompt_sha256` differs per sentence
as expected and is identical across repeated runs of the same sentence.

## Unknowns

- Which upstream model serves `fyp-opus`: response ids look Anthropic-style (`chatcmpl-msg_…`) and Claude
  Code reports `claude-opus-5-5`, but the gateway does not expose a model card.
- Why `tailscale status` calls the gateway peer offline while proxied requests return 200 (stale status,
  relay, or subnet route); not investigated.
- Whether `json_schema` is unsupported by the gateway as a whole or only by the `/chat/completions`
  translation; both 4000 and 8899 reject it identically.
- One sample sentence per pattern only; Chinese→English task translation was correct here but was not
  tested across varied phrasings.

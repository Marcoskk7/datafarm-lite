# Task identifiers and versioned harness storage

## Scope

New logical IDs reuse `kernel.schema.IDENTIFIER_PATTERN` without changing the
public IR or A-line schemas:

```text
^[a-z0-9][a-z0-9._:-]{0,127}$
```

Validation uses `fullmatch`: no case folding, punctuation replacement, truncation,
or node renaming occurs. `task.a:b` and `task.a_b` are distinct identities. Display
names, native joint/link names, provider session IDs and legacy broker request IDs
are separate protocols, not canonical task IDs.

This change implements harness/storage adoption only. Judge version adoption,
request planning, full Episode recording and action replay are subsequent features;
this branch does not claim that those flows are already implemented.

## Explicit opt-in

Existing `/1` adapters keep their lowercase-slug validation and existing record
versions. Existing constants and callers default to `/1`; nothing automatically
rewrites an adapter, round, suite or library record to `/2`.

A new adapter opts in with:

```json
{
  "schema": "datafarm.expert-harness.adapter/2",
  "identity_profile": "datafarm.logical-identity/1",
  "task_id": "task.pick:variant-1"
}
```

This is an identity fragment, not a runnable adapter. All existing required brief,
verifier, seeds, budgets, expert, runtime and isolation sections still apply.
The identity profile describes storage identity; it does not waive a frozen
verifier, change a budget or grant access to a protected file.

A `/2` suite likewise uses `datafarm.expert-harness.suite/2` and the explicit
identity profile. Its task IDs must be unique and must exactly match `/2` adapters.
Backend/model pairs must be unique; model strings retain their vendor spelling.
A `/1` suite cannot silently consume a `/2` adapter. Unknown versions fail closed.

## Logical IDs are not filenames

New storage keys have the form `<domain>-<full SHA-256>`. The digest covers canonical
JSON containing the domain, identity profile and original structured identity:

- `round`: task ID, prepared adapter SHA-256 and an attempt nonce;
- `suite`: suite ID and source specification SHA-256;
- `cell`: suite ID/digest, task ID, adapter digest, backend, model and repeat;
- `expert`: task ID, complete source digest, verifier version and round ID.

No delimiter-joined strings, lossy filename substitution, digest shortening or
round-ID suffix truncation is used for `/2` storage. Canonical IDs such as `con`,
`aux`, `task.` and IDs containing `:` remain valid logical values, but are never
used as generated filename components. Stored identities are retained and the
key is recomputed on lookup. Changed identity, unknown schema, mismatched filename,
unsafe lookup path or differing immutable receipt is rejected, not overwritten.
The expert source object keeps its existing complete content-addressed digest.

`prepare_round` with an adapter/2 writes `round/2`. Its optional `round_id` argument
is an explicit canonical **attempt nonce** for this version; the returned round ID
is the computed storage key. Omitting the nonce creates a new UUID nonce. The
operator's `round_dir` is an explicit runtime locator, not a task identity and
not a portable ArtifactRef. Suite/2 generates directories from safe cell keys.

## Record binding and resumption

New suites write `suite-plan/2` and `suite-report/2`. Plans retain the bound source
specification and structured identities, validate the task/model/repeat denominator
and scoring policy, and check each round directory against the suite output root.
Runtime locators in local harness records are not portable IR documents.
Before executing a planned cell, the harness rechecks the source adapter, task ID,
holdout input and the complete planned cell set. Changed inputs require a new plan.
Already completed cells are not resubmitted on a suite resume.

Round loading and execution validate both the explicit record version and its
prepared adapter identity. State/round IDs must agree. Original adapter and harness
source pins are not bypassed: old records may need their original source snapshot
or a new round after runtime code changes.

Library admission from adapter/2 writes `library-entry/2`. Direct `ExpertLibrary.add`
opts in through its explicit `schema` argument. A repeat add can reuse a receipt
only if the complete receipt is identical. Changed measurements/cost/time under the
same identity are rejected; a genuinely new attempt needs a new round identity.
New receipts are published without replacing existing bytes, including concurrent
writers. Filesystems without the required hard-link publication support fail closed.
There is no overwrite fallback. Legacy records are read with their own version;
source digest and filename checks also apply to library lookups.

## Limits

- These checks detect identity inconsistency; they do not authenticate a producer
  or create a malicious-code sandbox. A separate review execution is still required.
- Kernel helpers use only the standard library and kernel imports. This branch does
  not fix main's existing POSIX-only `fcntl` imports; native Windows execution of
  the entire harness is not claimed. Safe keys and immutable publication can be
  checked independently on Windows without claiming full harness portability.
- No legacy Judge freeze is rewritten and no previous dependency digest is asserted
  to remain valid. A new Judge runtime requires new, explicitly versioned freezing.
- No stage-graph executor, simulator result, model success or physical task quality
  follows from the identifier/storage tests.

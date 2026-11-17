# Request-bound Judge Episode contract

Request-task/1 is normalized without guessing IDs from display names. A new
judge-build-input/1 creates task-contract/1 and judge-freeze/1. Author and reviewer
remain independent recorded executions; candidate runtime rules are review data,
reviewer emits review only and pass requires empty issues. Runtime changes require
a new build directory/version, never rewriting an old freeze.

Measured scene Episodes project to judge-episode/1 with complete states and
explicit measurement paths encoded as arrays of key segments. A dotted node ID
remains one key. No final-state-only truncation; unavailable required evidence is
error, not false. Worker size and execution resource limits are unchanged.

Trusted `contract_binding` stores portable task/scene/request/source refs plus an
external freeze anchor. Frozen deployment locators stay in the opaque local
freeze blob, not portable document fields. Evaluation verifies the live bundle
and exact Episode/expert/contract refs and creates a separate immutable receipt.
The recorder does not certify success. Partial, aborted or infrastructure-failed
execution is not a normal judged task failure. No legacy/current Judge version selection is provided. Existing boolean
verdict producers remain a separate harness path. Earlier /1 input layouts are
not silently converted to the current source-bound contract. PRoot does not establish Judge secrecy.

Keeping the /1 label does not preserve old frozen hashes: changed runtime/source
requires fresh generation, independent review and a new build/freeze identity.

The portable authority stores both the original frozen TaskContract and Judge
source by content digest. Conditions, thresholds, bases and requirement bindings
must match those frozen bytes; changing portable JSON does not authorize new
criteria. The live bundle check also revalidates its normalized request/scene
inputs and all protected runtime dependencies.

Interrupted captures can retain an unmatched applied input and a missing tail
state. The host validates their source, clocks and existing readings, then emits
aborted or error with success=null without executing task predicates. Only a
completed, complete trajectory can receive success/failure. Unknown contacts
remain unknown; declaring an observation path does not prove sensor qualification.

Local build fixtures remain ready_for_cap=false. The explicit Python API
allow_fixture switch is for software checks only, is not exposed by the CLI, and
marks receipts software_fixture. It never certifies real model independence,
physical task success or production data acceptance.

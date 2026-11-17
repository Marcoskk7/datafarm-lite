# Request-bound Judge Episode contract

Request-task/1 is normalized without guessing IDs from display names. A new
judge-build-input/2 creates task-contract/2 and judge-freeze/2. Author and reviewer
remain independent recorded executions; candidate runtime rules are review data,
reviewer emits review only and pass requires empty issues. Runtime changes require
a new build directory/version, never rewriting an old freeze.

Measured scene Episodes project to judge-episode/2 with complete states and
explicit measurement paths encoded as arrays of key segments. A dotted node ID
remains one key. No final-state-only truncation; unavailable required evidence is
error, not false. Worker size and execution resource limits are unchanged.

Trusted `contract_binding` stores portable task/scene/request/source refs plus an
external freeze anchor. Frozen deployment locators stay in the opaque local
freeze blob, not portable document fields. Evaluation verifies the live bundle
and exact Episode/expert/contract refs and creates a separate immutable receipt.
The recorder does not certify success. Partial, aborted or infrastructure-failed
execution is not a normal judged task failure. Legacy judge-episode/1 and boolean
verdict paths remain explicit. PRoot does not establish Judge secrecy.

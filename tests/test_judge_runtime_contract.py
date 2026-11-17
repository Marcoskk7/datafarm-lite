"""The judge author/reviewer prompt must state every field the host episode validator enforces.

lab30, first real judge build: the independent reviewer's cases were rejected before judging
("contacts is missing required fields ['sampling', 'control_step']", "Expected N actions and N+1
full measured states") because the runtime contract text omitted those rules.
"""
from datafarm.ir.episode_state import STATE_KEYS
from datafarm.judge.provider import RUNTIME_CONTRACT


def test_contract_names_every_validated_state_and_contact_field():
    for key in STATE_KEYS:
        assert key in RUNTIME_CONTRACT
    for fragment in ("sampling='cycle_end_snapshot'", "contacts{control_step", "control_step exactly 0..N",
                     "exactly N actions", "schema_version='judge-episode/2'", "N >= 1"):
        assert fragment in RUNTIME_CONTRACT, fragment

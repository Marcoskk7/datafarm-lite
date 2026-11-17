"""Trusted replay entry: immutable Episode in, separate content-bound report out."""
from datafarm.expert.replay import verify_action_replay
from datafarm.kernel.artifacts import ArtifactRef


def replay_episode(store, episode_ref, host, tolerances, *, verifier_id):
    reference = ArtifactRef.from_dict(episode_ref)
    if reference.kind != "episode-ir":
        raise ValueError("Episode IR reference required")
    document = store.document(reference)
    report = verify_action_replay(document.payload, host, tolerances, verifier_id=verifier_id)
    report["episode_ref"] = reference.to_dict()
    receipt = store.put_json("action-replay-report", report)
    return report, receipt

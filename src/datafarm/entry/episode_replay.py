"""Trusted replay entry: immutable Episode in, separate content-bound report out."""
from datafarm.expert.replay import verify_action_replay
from datafarm.kernel.artifacts import ArtifactRef


def replay_episode(store, episode_ref, host, tolerances, *, verifier_id):
    from datafarm.kernel.schema import validate_ref
    validate_ref(episode_ref, "episode_ref")
    reference = ArtifactRef.from_dict(episode_ref)
    if reference.kind != "episode-ir":
        raise ValueError("Episode IR reference required")
    document = store.document(reference)
    report = verify_action_replay(document.payload, host, tolerances, verifier_id=verifier_id)
    report["episode_ref"] = reference.to_dict()
    from pathlib import Path
    from datafarm.kernel.io import sha256_file
    import datafarm.expert.replay as evaluator
    report["verifier_source_sha256"] = {"entry": sha256_file(Path(__file__)),
                                       "comparison": sha256_file(Path(evaluator.__file__))}
    receipt = store.put_json("action-replay-report", report)
    return report, receipt

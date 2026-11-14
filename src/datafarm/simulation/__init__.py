"""模拟器资质认定、随机化计划、joint margin 审计以及 USDCraft 资源桥接。"""

__all__ = [
    "CameraFramePerturbation",
    "CameraPerturbation",
    "CameraRandomizationPolicy",
    "CameraRealizationAudit",
    "EpisodeRandomizationPlan",
    "GraspPerturbation",
    "GraspRandomizationPolicy",
    "PosePerturbationBounds",
    "ProbeOutcome",
    "QualificationDecision",
    "RecoveryAudit",
    "RecoveryTrace",
    "SimulatorVariant",
    "VariantResult",
    "apply_patch",
    "audit_camera_realization",
    "audit_lateral_realign_trace",
    "audit_recovery_trace",
    "build_certificate",
    "canonical_json_sha256",
    "default_camera_randomization_policy",
    "evaluate_fold_probe",
    "evaluate_hold_probe",
    "sample_episode_randomization",
    "select_variant",
    "verify_certificate",
    "verify_run_binding"
]

# Keep independent discovery usable without POSIX-only broker imports.
_EXPORT_MODULES = {
    "QualificationDecision": "datafarm.simulation.sim_qualification",
    "ProbeOutcome": "datafarm.simulation.sim_qualification",
    "SimulatorVariant": "datafarm.simulation.sim_qualification",
    "VariantResult": "datafarm.simulation.sim_qualification",
    "apply_patch": "datafarm.simulation.sim_qualification",
    "build_certificate": "datafarm.simulation.sim_qualification",
    "evaluate_fold_probe": "datafarm.simulation.sim_qualification",
    "evaluate_hold_probe": "datafarm.simulation.sim_qualification",
    "select_variant": "datafarm.simulation.sim_qualification",
    "canonical_json_sha256": "datafarm.simulation.sim_qualification",
    "verify_certificate": "datafarm.simulation.sim_qualification",
    "verify_run_binding": "datafarm.simulation.sim_qualification",
    "CameraPerturbation": "datafarm.simulation.randomization",
    "CameraFramePerturbation": "datafarm.simulation.randomization",
    "CameraRealizationAudit": "datafarm.simulation.randomization",
    "CameraRandomizationPolicy": "datafarm.simulation.randomization",
    "EpisodeRandomizationPlan": "datafarm.simulation.randomization",
    "GraspPerturbation": "datafarm.simulation.randomization",
    "GraspRandomizationPolicy": "datafarm.simulation.randomization",
    "PosePerturbationBounds": "datafarm.simulation.randomization",
    "RecoveryAudit": "datafarm.simulation.randomization",
    "RecoveryTrace": "datafarm.simulation.randomization",
    "audit_lateral_realign_trace": "datafarm.simulation.randomization",
    "audit_recovery_trace": "datafarm.simulation.randomization",
    "audit_camera_realization": "datafarm.simulation.randomization",
    "default_camera_randomization_policy": "datafarm.simulation.randomization",
    "sample_episode_randomization": "datafarm.simulation.randomization",
}

def __getattr__(name):
    from importlib import import_module
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value

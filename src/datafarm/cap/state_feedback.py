"""CaP privileged physics feedback: measured Episode IR `state_trace` entries, never judge or holdout internals.

Each primitive's `before`/`after` is one state entry exactly as the recorder writes it
(`datafarm.ir.episode_state`); the feedback envelope carries the version marker.
"""
from datafarm.ir.episode_state import validate_state

SCHEMA_VERSION = 'privileged-state/2'
UNAVAILABLE = {'schema_version': SCHEMA_VERSION, 'availability': 'unavailable',
               'reason': 'expert_error_before_first_primitive'}


def validate_feedback(value):
    """Validate a `privileged_state` envelope as attached to a public CaP episode."""
    if value == UNAVAILABLE:
        return value
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'availability', 'initial', 'final'}
            or value['schema_version'] != SCHEMA_VERSION or value['availability'] != 'available'):
        raise ValueError('Executor did not provide privileged state feedback')
    validate_state(value['initial']); validate_state(value['final'])
    if value['final']['control_step'] < value['initial']['control_step']:
        raise ValueError('Primitive state clock moved backwards')
    return value


def feedback(trace, expert_error=False):
    if not trace:
        if expert_error:
            return dict(UNAVAILABLE)
        raise ValueError('Privileged state requested but no measured primitive states recorded')
    for row in trace:
        validate_state(row.get('before')); validate_state(row.get('after'))
        if row['after']['control_step'] < row['before']['control_step']:
            raise ValueError('Primitive state clock moved backwards')
    return {'schema_version': SCHEMA_VERSION, 'availability': 'available',
            'initial': trace[0]['before'], 'final': trace[-1]['after']}

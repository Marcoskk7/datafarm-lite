"""Public privileged physics state contract; never judge or holdout internals."""
import math


def validate_state(state):
    allowed = {'schema_version','control_step','time_s','frame_id','units','joint_names','joint_types',
               'q','dq','tcp_pose','objects','articulations','gripper','contacts','validity'}
    if (not isinstance(state, dict) or set(state)-allowed
            or state.get('schema_version') != 'privileged-state/1'
            or type(state.get('control_step')) is not int or state['control_step'] < 0
            or state.get('units') != {'length':'m','angle':'rad','time':'s'}
            or state.get('frame_id') != 'world'):
        raise ValueError('Expected versioned public privileged physics state with units and step')
    def finite(item):
        if isinstance(item,float) and not math.isfinite(item): raise ValueError('Nonfinite privileged state')
        if isinstance(item,dict):
            for value in item.values(): finite(value)
        if isinstance(item,list):
            for value in item: finite(value)
    finite(state)
    return state


def feedback(trace, expert_error=False):
    if not trace:
        if expert_error:
            return {'availability':'unavailable','reason':'expert_error_before_first_primitive'}
        raise ValueError('Privileged state requested but no measured primitive states recorded')
    for row in trace:
        validate_state(row.get('before')); validate_state(row.get('after'))
        if row['after']['control_step'] < row['before']['control_step']:
            raise ValueError('Primitive state clock moved backwards')
    return {'availability':'available','initial':trace[0]['before'],'final':trace[-1]['after']}

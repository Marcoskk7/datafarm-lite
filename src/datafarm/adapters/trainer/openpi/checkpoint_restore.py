"""把冻结 trainer 的状态恢复到请求的 device mesh 上。"""


def with_target_sharding(state, sharding):
    import jax

    return jax.tree.map(
        lambda value, target: jax.ShapeDtypeStruct(
            value.shape, value.dtype, sharding=target
        ),
        state,
        sharding,
    )


def restore_state(checkpoint_manager, state, data_loader, step=None):
    """保持参数/optimizer/计数器不变，覆盖保存的 device 布局。"""
    del data_loader
    import jax
    import orbax.checkpoint as ocp
    from orbax.checkpoint import checkpoint_utils
    from openpi.shared import array_typing as at
    from openpi.training import checkpoints

    if any(getattr(x, "sharding", None) is None for x in jax.tree.leaves(state)):
        raise ValueError("Resume requires an explicit target sharding for every state leaf")
    with at.disable_typechecking():
        train_state, params = checkpoints._split_params(state)
        items = {"train_state": train_state, "params": {"params": params}}
        restored = checkpoint_manager.restore(
            step,
            args=ocp.args.Composite(**{
                name: ocp.args.PyTreeRestore(
                    item=item,
                    restore_args=checkpoint_utils.construct_restore_args(item),
                )
                for name, item in items.items()
            }),
        )
    return checkpoints._merge_params(restored["train_state"], restored["params"])


def install(trainer_globals):
    """只修补运行时钩子；锁定版本的 trainer 源码保持只读。"""
    original_init = trainer_globals["init_train_state"]

    def init_train_state(config, init_rng, mesh, *, resume):
        state, sharding = original_init(config, init_rng, mesh, resume=resume)
        if resume:
            state = with_target_sharding(state, sharding)
        return state, sharding

    trainer_globals["init_train_state"] = init_train_state
    trainer_globals["_checkpoints"].restore_state = restore_state

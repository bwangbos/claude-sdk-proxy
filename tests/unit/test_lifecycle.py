from __future__ import annotations

from dataclasses import replace

import pytest

from claude_sdk_proxy.lifecycle import (
    ALL_COMPLETED_STEPS,
    BatchDescriptor,
    BatchDescriptorKind,
    BatchOutcome,
    IllegalTransition,
    Lifecycle,
    ProcessIdentity,
    Record,
    State,
    StateKind,
)


def _bound(state: State) -> State:
    return replace(
        state,
        workdir_parent_dev=1,
        workdir_parent_ino=2,
        workdir_dev=3,
        workdir_ino=4,
        workdir_bound=True,
        workdir_name="allocation.workdir",
    )


_TARGET = ProcessIdentity(
    pid=123,
    start_ns=456,
    uid=789,
    pgid=123,
    sid=123,
    flags=63,
    executable_dev=10,
    executable_ino=11,
    boot_id=b"b" * 32,
    executable_hash=b"e" * 32,
)
_PROCESS_ABSENT = BatchDescriptor(BatchDescriptorKind.PROCESS_ABSENT, 0, _TARGET)
_REAP_PROCESS = BatchDescriptor(BatchDescriptorKind.REAP_PROCESS, 1, _TARGET)
_REMOVE_WORKDIR = BatchDescriptor(BatchDescriptorKind.REMOVE_WORKDIR, 3)
_TERMINAL_CHECKS = BatchDescriptor(BatchDescriptorKind.TERMINAL_CHECKS, 7)


def test_no_generation_may_prepare_exact_candidate() -> None:
    prepared = Lifecycle.apply(
        _bound(State.no_generation()),
        Record.prepared(1, "helper-1", claim_deadline_ns=100),
    )

    assert prepared == _bound(
        State.prepared(1, "helper-1", claim_deadline_ns=100)
    )


def test_prepared_activates_only_the_exact_candidate() -> None:
    prepared = _bound(State.prepared(1, "helper-1", claim_deadline_ns=100))

    active = Lifecycle.apply(
        prepared,
        Record.active_ready(1, "helper-1", lease_deadline_ns=200),
    )
    assert active == _bound(
        State.active_ready(1, "helper-1", lease_deadline_ns=200)
    )

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            prepared,
            Record.active_ready(1, "helper-2", lease_deadline_ns=200),
        )


def test_expired_prepared_candidate_retires_idle() -> None:
    prepared = State.prepared(1, "helper-1", claim_deadline_ns=100)

    retired = Lifecycle.apply(
        prepared,
        Record.retiring_idle(
            1,
            "helper-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=300,
        ),
    )

    assert retired.kind is StateKind.RETIRING_IDLE
    assert retired.prior_actor == "helper-1"
    assert retired.authority == "reconciler-1"


def test_active_executor_admits_and_completes_repeatable_batches() -> None:
    active = _bound(State.active_ready(2, "executor-1", lease_deadline_ns=500))

    batch = Lifecycle.apply(
        active,
        Record.batch_active(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=0,
            descriptors=(_PROCESS_ABSENT,),
        ),
    )
    assert batch.kind is StateKind.BATCH_ACTIVE

    again = Lifecycle.apply(
        batch,
        Record.batch_done(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=1,
        ),
    )
    second = Lifecycle.apply(
        again,
        Record.batch_active(
            2,
            "batch-2",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=1,
            descriptors=(_REAP_PROCESS,),
        ),
    )

    assert again == _bound(State.active_ready(
        2,
        "executor-1",
        lease_deadline_ns=500,
        completed_steps=1,
    ))
    assert second.exact_batch == "batch-2"


def test_completed_batch_must_advance_every_admitted_descriptor_step() -> None:
    active = _bound(State.active_ready(2, "executor-1", lease_deadline_ns=500))
    batch = Lifecycle.apply(
        active,
        Record.batch_active(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            descriptors=(_PROCESS_ABSENT,),
        ),
    )

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            batch,
            Record.batch_done(
                2,
                "batch-1",
                executor="executor-1",
                lease_deadline_ns=500,
                completed_steps=0,
            ),
        )


def test_fixed_cleanup_steps_bound_recovery_to_four_complete_batches() -> None:
    state = replace(
        _bound(State.active_ready(2, "executor-1", lease_deadline_ns=500)),
        authority_epoch=2,
    )
    completed_steps = 0
    descriptors = (
        _PROCESS_ABSENT,
        _REAP_PROCESS,
        _REMOVE_WORKDIR,
        _TERMINAL_CHECKS,
    )

    for index, descriptor in enumerate(descriptors, start=1):
        batch_nonce = f"batch-{index}"
        active = Lifecycle.apply(
            state,
            Record.batch_active(
                2,
                batch_nonce,
                executor="executor-1",
                lease_deadline_ns=500,
                completed_steps=completed_steps,
                descriptors=(descriptor,),
            ),
        )
        completed_steps |= 1 << (index - 1)
        state = Lifecycle.apply(
            active,
            Record.batch_done(
                2,
                batch_nonce,
                executor="executor-1",
                lease_deadline_ns=500,
                completed_steps=completed_steps,
            ),
        )

    assert completed_steps == ALL_COMPLETED_STEPS
    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            state,
            Record.batch_active(
                2,
                "batch-5",
                executor="executor-1",
                lease_deadline_ns=500,
                completed_steps=completed_steps,
                descriptors=(_TERMINAL_CHECKS,),
            ),
        )


def test_batch_cycle_preserves_recorded_process_identity() -> None:
    active = State(
        StateKind.ACTIVE_READY,
        generation=2,
        lease_deadline_ns=500,
        executor="executor-1",
        process_pid=123,
        process_start_ns=456,
        process_uid=789,
        process_pgid=123,
        process_sid=123,
        process_identity_flags=7,
        executable_dev=10,
        executable_ino=11,
        boot_id=b"b" * 32,
        executable_hash=b"e" * 32,
        workdir_parent_dev=1,
        workdir_parent_ino=2,
        workdir_dev=3,
        workdir_ino=4,
        workdir_bound=True,
        workdir_name="allocation.workdir",
    )

    batch = Lifecycle.apply(
        active,
        Record.batch_active(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            descriptors=(_PROCESS_ABSENT,),
        ),
    )
    again = Lifecycle.apply(
        batch,
        Record.batch_done(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=1,
        ),
    )

    for state in (batch, again):
        assert state.process_pid == 123
        assert state.process_start_ns == 456
        assert state.process_uid == 789
        assert state.process_pgid == 123
        assert state.process_sid == 123
        assert state.process_identity_flags == 7
        assert state.executable_dev == 10
        assert state.executable_ino == 11
        assert state.boot_id == b"b" * 32
        assert state.executable_hash == b"e" * 32


def test_active_without_admitted_batch_retires_idle() -> None:
    active = State.active_ready(2, "executor-1", lease_deadline_ns=500)

    retired = Lifecycle.apply(
        active,
        Record.retiring_idle(
            2,
            "executor-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=800,
            exact_batch="fabricated-batch",
            batch_outcome=BatchOutcome.COMPLETED,
        ),
    )

    assert retired.kind is StateKind.RETIRING_IDLE
    assert retired.exact_batch == ""
    assert retired.inherited_batch == ""
    assert retired.batch_outcome is BatchOutcome.NONE


def test_retiring_batch_preserves_only_admitted_batch() -> None:
    active = State.batch_active(
        generation=2,
        batch_nonce="b1",
        executor="executor-1",
        lease_deadline_ns=500,
    )
    retired = Lifecycle.apply(
        active,
        Record.retiring_batch(
            2,
            "b1",
            prior_executor="executor-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=800,
        ),
    )
    assert retired.exact_batch == "b1"

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            retired,
            Record.batch_active(
                2,
                "b2",
                executor="executor-1",
                lease_deadline_ns=500,
            ),
        )


@pytest.mark.parametrize("outcome", [BatchOutcome.COMPLETED, BatchOutcome.INTERRUPTED])
def test_retiring_batch_records_outcome_before_becoming_idle(
    outcome: BatchOutcome,
) -> None:
    retiring = State.retiring_batch(
        2,
        "batch-1",
        prior_executor="executor-1",
        authority="reconciler-1",
        authority_epoch=1,
        deadline_ns=800,
    )

    idle = Lifecycle.apply(
        retiring,
        Record.batch_done(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=0,
            batch_outcome=outcome,
        ),
    )

    assert idle.kind is StateKind.RETIRING_IDLE
    assert idle.exact_batch == "batch-1"
    assert idle.batch_outcome is outcome


@pytest.mark.parametrize(
    "retiring",
    [
        State.retiring_idle(
            2,
            "executor-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=800,
        ),
        State.retiring_batch(
            2,
            "batch-1",
            prior_executor="executor-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=800,
        ),
    ],
)
def test_expired_retirement_authority_can_be_replaced(retiring: State) -> None:
    replacement = Lifecycle.apply(
        retiring,
        Record.replace_authority(
            authority="reconciler-2",
            authority_epoch=2,
            deadline_ns=900,
        ),
    )

    assert replacement.kind is retiring.kind
    assert replacement.generation == retiring.generation
    assert replacement.prior_actor == retiring.prior_actor
    assert replacement.exact_batch == retiring.exact_batch
    assert replacement.batch_outcome is retiring.batch_outcome
    assert replacement.authority == "reconciler-2"
    assert replacement.authority_epoch == 2

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            replacement,
            Record.replace_authority(
                authority="reconciler-3",
                authority_epoch=3,
                deadline_ns=1_000,
            ),
        )


def test_retiring_idle_hands_off_to_exact_next_generation() -> None:
    retired = _bound(State.retiring_idle(
        2,
        "executor-1",
        authority="reconciler-1",
        authority_epoch=1,
        deadline_ns=800,
        exact_batch="batch-1",
        batch_outcome=BatchOutcome.INTERRUPTED,
    ))

    prepared = Lifecycle.apply(
        retired,
        Record.prepared(3, "helper-2", claim_deadline_ns=900),
    )

    assert prepared == _bound(State.prepared(
        3,
        "helper-2",
        claim_deadline_ns=900,
        inherited_batch="batch-1",
    ))

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            retired,
            Record.prepared(4, "helper-3", claim_deadline_ns=900),
        )


def test_completed_retiring_batch_is_not_inherited_by_successor() -> None:
    retiring = _bound(State.retiring_batch(
        2,
        "batch-1",
        prior_executor="executor-1",
        authority="reconciler-1",
        authority_epoch=1,
        deadline_ns=800,
    ))
    idle = Lifecycle.apply(
        retiring,
        Record.batch_done(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=0,
            batch_outcome=BatchOutcome.COMPLETED,
        ),
    )

    prepared = Lifecycle.apply(
        idle,
        Record.prepared(3, "helper-2", claim_deadline_ns=900),
    )

    assert prepared.inherited_batch == ""


def test_done_requires_all_terminal_steps_and_no_admitted_batch() -> None:
    active = State.active_ready(
        2,
        "executor-1",
        lease_deadline_ns=500,
        completed_steps=ALL_COMPLETED_STEPS,
    )

    done = Lifecycle.apply(active, Record.done(2, "executor-1"))
    assert done.kind is StateKind.DONE

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            State.active_ready(
                2,
                "executor-1",
                lease_deadline_ns=500,
                completed_steps=ALL_COMPLETED_STEPS - 1,
            ),
            Record.done(2, "executor-1"),
        )
    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            State.batch_active(
                2,
                "batch-1",
                executor="executor-1",
                lease_deadline_ns=500,
                completed_steps=ALL_COMPLETED_STEPS,
            ),
            Record.done(2, "executor-1"),
        )


@pytest.mark.parametrize(
    "state",
    [
        State.no_generation(),
        State.prepared(1, "helper-1", claim_deadline_ns=100),
        State.active_ready(1, "helper-1", lease_deadline_ns=200),
        State.batch_active(
            1,
            "batch-1",
            executor="helper-1",
            lease_deadline_ns=200,
        ),
        State.retiring_idle(
            1,
            "helper-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=300,
        ),
        State.retiring_batch(
            1,
            "batch-1",
            prior_executor="helper-1",
            authority="reconciler-1",
            authority_epoch=1,
            deadline_ns=300,
        ),
    ],
)
def test_any_non_done_state_can_fail_closed_to_unconfirmed(state: State) -> None:
    unconfirmed = Lifecycle.apply(state, Record.unconfirmed("proof unavailable"))

    assert unconfirmed.kind is StateKind.UNCONFIRMED
    assert unconfirmed.retained_kind is state.kind
    assert unconfirmed.reason == "proof unavailable"
    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            unconfirmed,
            Record.prepared(2, "helper-2", claim_deadline_ns=400),
        )


def test_done_is_terminal() -> None:
    done = State.done(1, "executor-1")

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(done, Record.unconfirmed("late failure"))


def test_unbound_or_absence_certified_bootstrap_cannot_prepare() -> None:
    for state in (
        State.no_generation(),
        State(StateKind.NO_GENERATION, no_dependent_artifact=True),
    ):
        with pytest.raises(IllegalTransition):
            Lifecycle.apply(
                state,
                Record.prepared(1, "helper-1", claim_deadline_ns=100),
            )


def test_active_ready_revalidates_certified_workdir_prerequisite() -> None:
    prepared_without_binding = State.prepared(
        1,
        "helper-1",
        claim_deadline_ns=100,
    )

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            prepared_without_binding,
            Record.active_ready(1, "helper-1", lease_deadline_ns=200),
        )

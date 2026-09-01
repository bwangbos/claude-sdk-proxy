from __future__ import annotations

import pytest

from claude_sdk_proxy.lifecycle import (
    ALL_COMPLETED_STEPS,
    BatchOutcome,
    IllegalTransition,
    Lifecycle,
    Record,
    State,
    StateKind,
)


def test_no_generation_may_prepare_exact_candidate() -> None:
    prepared = Lifecycle.apply(
        State.no_generation(),
        Record.prepared(1, "helper-1", claim_deadline_ns=100),
    )

    assert prepared == State.prepared(1, "helper-1", claim_deadline_ns=100)


def test_prepared_activates_only_the_exact_candidate() -> None:
    prepared = State.prepared(1, "helper-1", claim_deadline_ns=100)

    active = Lifecycle.apply(
        prepared,
        Record.active_ready(1, "helper-1", lease_deadline_ns=200),
    )
    assert active == State.active_ready(1, "helper-1", lease_deadline_ns=200)

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
    active = State.active_ready(2, "executor-1", lease_deadline_ns=500)

    batch = Lifecycle.apply(
        active,
        Record.batch_active(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
            completed_steps=0,
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
        ),
    )

    assert again == State.active_ready(
        2,
        "executor-1",
        lease_deadline_ns=500,
        completed_steps=1,
    )
    assert second.exact_batch == "batch-2"


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
    )

    batch = Lifecycle.apply(
        active,
        Record.batch_active(
            2,
            "batch-1",
            executor="executor-1",
            lease_deadline_ns=500,
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


def test_retiring_idle_hands_off_to_exact_next_generation() -> None:
    retired = State.retiring_idle(
        2,
        "executor-1",
        authority="reconciler-1",
        authority_epoch=1,
        deadline_ns=800,
        exact_batch="batch-1",
        batch_outcome=BatchOutcome.INTERRUPTED,
    )

    prepared = Lifecycle.apply(
        retired,
        Record.prepared(3, "helper-2", claim_deadline_ns=900),
    )

    assert prepared == State.prepared(
        3,
        "helper-2",
        claim_deadline_ns=900,
        inherited_batch="batch-1",
    )

    with pytest.raises(IllegalTransition):
        Lifecycle.apply(
            retired,
            Record.prepared(4, "helper-3", claim_deadline_ns=900),
        )


def test_completed_retiring_batch_is_not_inherited_by_successor() -> None:
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

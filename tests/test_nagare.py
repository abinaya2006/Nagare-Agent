"""Focused tests for Nagare's deterministic scheduling core."""
from datetime import datetime

import pytest
from pydantic import ValidationError

from demo.nagare.flow import build_flow
from demo.nagare.planner import (
    baseline_schedule,
    candidate_slots,
    fragment_task,
    reschedule_task,
)
from demo.nagare.schema import (
    CircadianProfile,
    ProposedSchedule,
    ScheduleBlock,
    Task,
    TimeWindow,
    UserScheduleProfile,
)
from demo.nagare.validator import validate_schedule
from slice import callback, runner
from slice.config import Settings
from slice.records import RunState
from slice.store import Store


DAY = datetime(2026, 9, 19)


def window(start_hour: int, end_hour: int) -> TimeWindow:
    return TimeWindow(
        start=DAY.replace(hour=start_hour),
        end=DAY.replace(hour=end_hour),
    )


def profile(*available: TimeWindow) -> UserScheduleProfile:
    return UserScheduleProfile(
        available_windows=list(available),
        circadian_profile=CircadianProfile(
            morning_energy=5,
            afternoon_energy=3,
            evening_energy=2,
        ),
        preferred_work_periods=[window(9, 12)],
        protected_blocks=[],
        preferred_session_length=60,
        preferred_break_length=30,
        sleep_window=TimeWindow(
            start=DAY.replace(hour=23),
            end=DAY.replace(hour=23) +
            __import__("datetime").timedelta(hours=8),
        ),
        commute_windows=[],
    )


def task(task_id: str = "ml", duration: int = 120, **overrides) -> Task:
    values = {
        "id": task_id,
        "title": "ML assignment",
        "estimated_duration": duration,
        "movable": True,
    }
    values.update(overrides)
    return Task(**values)


def block(block_id: str, start_hour: int, end_hour: int, **overrides) -> ScheduleBlock:
    values = {
        "id": block_id,
        "task_id": block_id,
        "start": DAY.replace(hour=start_hour),
        "end": DAY.replace(hour=end_hour),
        "block_type": "task",
    }
    values.update(overrides)
    return ScheduleBlock(**values)


def test_baseline_schedule_prioritizes_higher_priority_tasks():
    schedule = baseline_schedule(
        [
            task("low", 60, title="Low priority", priority=1),
            task("high", 60, title="High priority", priority=5),
        ],
        profile(window(9, 12)),
    )

    assert [item.task_id for item in schedule.blocks] == ["high", "low"]


def test_baseline_schedule_uses_priority_before_deadline():
    schedule = baseline_schedule(
        [
            task("urgent", 60, priority=5, deadline=DAY.replace(hour=17),
                 deadline_type="hard"),
            task("soon", 60, priority=3, deadline=DAY.replace(hour=12),
                 deadline_type="hard"),
        ],
        profile(window(9, 12)),
    )

    assert [item.task_id for item in schedule.blocks] == ["urgent", "soon"]


def test_higher_priority_assignment_precedes_lower_priority_shopping():
    schedule = baseline_schedule(
        [
            task("assignment", 60, priority=4,
                 deadline=DAY.replace(hour=18), deadline_type="hard"),
            task("shopping", 150, priority=3,
                 deadline=DAY.replace(hour=17), deadline_type="hard"),
            task("leetcode", 100, priority=4,
                 deadline=DAY.replace(hour=14), deadline_type="hard"),
        ],
        profile(window(9, 21)),
    )

    assert [item.task_id for item in schedule.blocks] == [
        "leetcode", "assignment", "shopping"
    ]


def test_baseline_schedule_uses_prior_user_answer_for_undated_ties():
    schedule = baseline_schedule(
        [
            task("essay", 60, title="Write essay", priority=3),
            task("slides", 60, title="Prepare slides", priority=3),
        ],
        profile(window(9, 12)),
        user_responses=["Prepare slides first"],
    )

    assert [item.task_id for item in schedule.blocks] == ["slides", "essay"]


def test_planner_does_not_use_protected_time():
    user_profile = profile(window(9, 17))
    user_profile.protected_blocks = [window(10, 12)]

    slots = candidate_slots(task(duration=120), 120, user_profile, [])

    assert slots
    assert all(not (start < DAY.replace(hour=12) and end > DAY.replace(hour=10))
               for start, end in slots)


def test_planner_reserves_configured_break_between_tasks():
    user_profile = profile(window(9, 12))
    user_profile.task_break_minutes = 15
    schedule = baseline_schedule(
        [task("first", 60), task("second", 60)],
        user_profile,
    )

    first, second = schedule.blocks
    assert second.start >= first.end + \
        __import__("datetime").timedelta(minutes=15)


def test_reschedule_moves_task_into_a_future_feasible_slot():
    user_profile = profile(window(9, 17))
    original = block("block_ml", 9, 11)

    schedule = reschedule_task(
        task(),
        remaining_minutes=120,
        existing=[original],
        tasks=[task()],
        profile=user_profile,
        excluded_block_id=original.id,
    )

    moved = next(item for item in schedule.blocks if item.task_id == "ml")
    assert moved.start == DAY.replace(hour=11)
    assert moved.end == DAY.replace(hour=13)
    assert moved.id == "block_ml"


def test_flow_reschedule_replaces_missed_block_instead_of_duplicating(tmp_path):
    store = Store(tmp_path / "replace-missed.db")
    run_id = store.create_run("nagare")
    user_profile = profile(window(9, 17))
    user_task = task("ml", duration=120)
    old_block = block("block_ml", 9, 11, task_id="ml")
    store.append(
        run_id,
        "input",
        {
            "profile": user_profile.model_dump(mode="json"),
            "tasks": [user_task.model_dump(mode="json")],
            "existing_blocks": [old_block.model_dump(mode="json")],
            "missed_task_id": "ml",
        },
        produced_by="test",
    )
    settings = Settings(
        api_key="",
        model="model",
        fallback_model="fallback",
        escalation_model="escalation",
        max_tokens=100,
        max_tokens_per_run=1000,
        max_attempts_per_step=2,
        expert_timeout_minutes=45,
        langfuse_public="",
        langfuse_secret="",
        langfuse_host="",
    )

    assert runner.advance(
        store, run_id, build_flow(), settings
    ) is RunState.COMPLETE
    blocks = store.latest(run_id, "decision")["schedule"]["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["task_id"] == "ml"
    assert blocks[0]["start"] == "2026-09-19T11:00:00"


def test_reschedule_preserves_locked_blocks():
    user_profile = profile(window(9, 17))
    locked = block("class", 13, 15, task_id=None,
                   block_type="protected", locked=True)

    schedule = reschedule_task(
        task(),
        remaining_minutes=120,
        existing=[locked],
        tasks=[task()],
        profile=user_profile,
    )

    assert locked in schedule.blocks
    assert not any(
        item.task_id == "ml" and item.start < locked.end and item.end > locked.start
        for item in schedule.blocks
    )


def test_validator_passes_a_valid_schedule():
    user_profile = profile(window(9, 12))
    scheduled = baseline_schedule([task(duration=120)], user_profile)

    result = validate_schedule(
        scheduled, [task(duration=120)], user_profile, [])

    assert result.status == "PASS"
    assert result.conflicts == []


def test_validator_blocks_tasks_missing_from_proposed_schedule():
    user_profile = profile(window(9, 10))
    scheduled = baseline_schedule([task(duration=90)], user_profile)

    result = validate_schedule(
        scheduled, [task(duration=90)], user_profile, [])

    assert result.status == "BLOCK"
    assert any(
        conflict.conflict_type == "unresolved_constraint"
        for conflict in result.conflicts
    )


def test_impossible_schedule_suspends_for_human_input(tmp_path):
    store = Store(tmp_path / "impossible-flow.db")
    run_id = store.create_run("nagare")
    user_profile = profile(window(9, 10))
    impossible = task("impossible", duration=120)
    store.append(
        run_id,
        "input",
        {
            "profile": user_profile.model_dump(mode="json"),
            "tasks": [impossible.model_dump(mode="json")],
            "existing_blocks": [],
        },
        produced_by="test",
    )
    settings = Settings(
        api_key="",
        model="model",
        fallback_model="fallback",
        escalation_model="escalation",
        max_tokens=100,
        max_tokens_per_run=1000,
        max_attempts_per_step=2,
        expert_timeout_minutes=45,
        langfuse_public="",
        langfuse_secret="",
        langfuse_host="",
    )

    assert runner.advance(
        store, run_id, build_flow(), settings
    ) is RunState.AWAITING_EXPERT
    assert callback.pending(store, run_id)


def test_validator_blocks_overlapping_tasks():
    user_profile = profile(window(9, 17))
    scheduled = type("Schedule", (), {"blocks": [
                     block("a", 9, 11), block("b", 10, 12)]})()

    result = validate_schedule(
        scheduled,
        [task("a", 120), task("b", 120)],
        user_profile,
        [],
    )

    assert result.status == "BLOCK"
    assert any(conflict.conflict_type ==
               "time_overlap" for conflict in result.conflicts)


def test_validator_detects_changed_locked_block():
    user_profile = profile(window(9, 17))
    locked = block("class", 13, 15, task_id=None,
                   block_type="protected", locked=True)
    changed = block("class", 14, 16, task_id=None,
                    block_type="protected", locked=True)
    scheduled = type("Schedule", (), {"blocks": [changed]})()

    result = validate_schedule(scheduled, [], user_profile, [locked])

    assert result.status == "BLOCK"
    assert any(conflict.conflict_type ==
               "locked_block" for conflict in result.conflicts)


def test_task_rejects_inconsistent_deadline_fields():
    with pytest.raises(ValidationError):
        task(deadline=DAY, deadline_type="none")


def test_injected_agent_draft_runs_through_validator_gate(tmp_path):
    store = Store(tmp_path / "agent.db")
    run_id = store.create_run("nagare")
    user_profile = profile(window(9, 12))
    user_task = task(duration=60)
    store.append(
        run_id,
        "input",
        {
            "profile": user_profile.model_dump(mode="json"),
            "tasks": [user_task.model_dump(mode="json")],
            "existing_blocks": [],
        },
        produced_by="test",
    )

    calls = []

    def fake_agent(**kwargs):
        calls.append(kwargs)
        return baseline_schedule([user_task], user_profile)

    settings = Settings(
        api_key="test",
        model="model",
        fallback_model="fallback",
        escalation_model="escalation",
        max_tokens=100,
        max_tokens_per_run=1000,
        max_attempts_per_step=2,
        expert_timeout_minutes=45,
        langfuse_public="",
        langfuse_secret="",
        langfuse_host="",
    )

    final_state = runner.advance(
        store, run_id, build_flow(fake_agent), settings)

    assert final_state is RunState.COMPLETE
    assert calls[0]["schema"] is ProposedSchedule
    assert store.latest(run_id, "decision")["status"] == "scheduled"
    assert store.history(run_id, "proposed_schedule")[
        0].produced_by == "agent:nagare_draft"


def test_incomplete_ai_schedule_uses_feasible_deadline_fallback(tmp_path):
    store = Store(tmp_path / "ai-incomplete.db")
    run_id = store.create_run("nagare")
    user_profile = profile(window(9, 12))
    user_task = task(
        duration=60,
        deadline=DAY.replace(hour=11),
        deadline_type="hard",
    )
    store.append(
        run_id,
        "input",
        {
            "profile": user_profile.model_dump(mode="json"),
            "tasks": [user_task.model_dump(mode="json")],
            "existing_blocks": [],
        },
        produced_by="test",
    )

    def incomplete_agent(**kwargs):
        return ProposedSchedule()

    final_state = runner.advance(
        store, run_id, build_flow(incomplete_agent), Settings(
            api_key="test",
            model="model",
            fallback_model="fallback",
            escalation_model="escalation",
            max_tokens=100,
            max_tokens_per_run=1000,
            max_attempts_per_step=2,
            expert_timeout_minutes=45,
            langfuse_public="",
            langfuse_secret="",
            langfuse_host="",
        ))

    assert final_state is RunState.COMPLETE
    assert store.latest(run_id, "decision")[
        "schedule"]["blocks"][0]["task_id"] == "ml"
    assert store.latest(run_id, "agent_fallback")[
        "kind"] == "incomplete_proposal"

    def test_empty_agent_proposal_uses_feasible_fallback(tmp_path):
        store = Store(tmp_path / "agent-fallback.db")
        run_id = store.create_run("nagare")
        user_profile = profile(window(9, 21))
        user_task = task(duration=90, deadline=DAY.replace(hour=18))
        store.append(
            run_id,
            "input",
            {
                "profile": user_profile.model_dump(mode="json"),
                "tasks": [user_task.model_dump(mode="json")],
                "existing_blocks": [],
            },
            produced_by="test",
        )

        def empty_agent(**kwargs):
            return ProposedSchedule()

        settings = Settings(
            api_key="test",
            model="model",
            fallback_model="fallback",
            escalation_model="escalation",
            max_tokens=100,
            max_tokens_per_run=1000,
            max_attempts_per_step=2,
            expert_timeout_minutes=45,
            langfuse_public="",
            langfuse_secret="",
            langfuse_host="",
        )

        assert runner.advance(
            store, run_id, build_flow(empty_agent), settings) is RunState.COMPLETE
        assert store.latest(run_id, "agent_fallback")[
            "kind"] == "incomplete_proposal"


def test_validator_requests_fragmentation_before_deadline():
    user_profile = profile(window(11, 21))
    urgent = task(
        duration=240,
        deadline=DAY.replace(hour=14),
        deadline_type="hard",
    )
    result = validate_schedule(
        baseline_schedule([urgent], user_profile),
        [urgent],
        user_profile,
        [],
    )

    assert result.status == "BLOCK"
    conflict = result.conflicts[0]
    assert conflict.conflict_type == "fragmentation"
    assert "split" in conflict.explanation.lower()


def test_fragmented_task_can_pass_before_deadline():
    user_profile = profile(window(11, 21))
    urgent = task(
        duration=180,
        deadline=DAY.replace(hour=14),
        deadline_type="hard",
        fragmentable=True,
        min_fragment_duration=60,
        preferred_fragment_duration=60,
    )
    schedule = fragment_task(urgent, user_profile)
    result = validate_schedule(schedule, [urgent], user_profile, [])

    assert len(schedule.blocks) == 3
    assert result.status == "PASS"


def test_fragmented_task_keeps_unplaced_remainder_pending():
    user_profile = profile(window(10, 12))
    urgent = task(
        duration=120,
        deadline=DAY.replace(hour=11, minute=30),
        deadline_type="hard",
    )

    schedule = fragment_task(urgent, user_profile)
    result = validate_schedule(schedule, [urgent], user_profile, [])

    assert sum(
        int((item.end - item.start).total_seconds() // 60)
        for item in schedule.blocks
        if item.task_id == urgent.id
    ) == 90
    assert schedule.pending_minutes == {urgent.id: 30}
    assert result.status == "PASS"


def test_split_replan_schedules_remaining_tasks_around_fragments():
    user_profile = profile(window(11, 17))
    user_profile.protected_blocks = [window(12, 13)]
    urgent = task(
        "urgent",
        duration=120,
        deadline=DAY.replace(hour=14),
        deadline_type="hard",
        fragmentable=True,
        min_fragment_duration=60,
        preferred_fragment_duration=60,
    )
    remaining = task("remaining", duration=60, priority=2)
    fragments = fragment_task(urgent, user_profile)
    schedule = baseline_schedule(
        [remaining],
        user_profile,
        existing=fragments.blocks,
    )

    assert [item.task_id for item in schedule.blocks] == [
        "urgent", "urgent", "remaining"
    ]
    assert schedule.blocks[-1].start == DAY.replace(hour=14)


def test_agent_loop_asks_and_resolves_fragmentation(tmp_path):
    store = Store(tmp_path / "fragment-loop.db")
    run_id = store.create_run("nagare")
    user_profile = profile(window(11, 14))
    user_profile.protected_blocks = [window(12, 13)]
    urgent = task(
        duration=120,
        deadline=DAY.replace(hour=14),
        deadline_type="hard",
        fragmentable=True,
        min_fragment_duration=60,
        preferred_fragment_duration=60,
    )
    store.append(
        run_id,
        "input",
        {
            "profile": user_profile.model_dump(mode="json"),
            "tasks": [urgent.model_dump(mode="json")],
            "existing_blocks": [],
        },
        produced_by="test",
    )
    settings = Settings(
        api_key="test",
        model="model",
        fallback_model="fallback",
        escalation_model="escalation",
        max_tokens=100,
        max_tokens_per_run=1000,
        max_attempts_per_step=2,
        expert_timeout_minutes=45,
        langfuse_public="",
        langfuse_secret="",
        langfuse_host="",
    )

    assert runner.advance(store, run_id, build_flow(),
                          settings) is RunState.AWAITING_EXPERT
    question = callback.pending(store, run_id)[0]
    assert "fragment" in question.question.lower()
    callback.answer(store, question.id, "fragment the task", who="user")

    assert runner.advance(store, run_id, build_flow(),
                          settings) is RunState.COMPLETE
    assert len(store.latest(run_id, "decision")["schedule"]["blocks"]) == 2


def test_split_button_bypasses_ai_and_updates_schedule(tmp_path):
    store = Store(tmp_path / "ai-fragment-loop.db")
    run_id = store.create_run("nagare")
    user_profile = profile(window(11, 14))
    user_profile.protected_blocks = [window(12, 13)]
    urgent = task(
        duration=120,
        deadline=DAY.replace(hour=14),
        deadline_type="hard",
        fragmentable=True,
        min_fragment_duration=60,
        preferred_fragment_duration=60,
    )
    store.append(
        run_id,
        "input",
        {
            "profile": user_profile.model_dump(mode="json"),
            "tasks": [urgent.model_dump(mode="json")],
            "existing_blocks": [],
        },
        produced_by="test",
    )

    calls = []

    def fake_agent(**kwargs):
        calls.append(kwargs)
        return ProposedSchedule()

    ai_settings = Settings(
        api_key="test",
        model="model",
        fallback_model="fallback",
        escalation_model="escalation",
        max_tokens=100,
        max_tokens_per_run=1000,
        max_attempts_per_step=2,
        expert_timeout_minutes=45,
        langfuse_public="",
        langfuse_secret="",
        langfuse_host="",
    )

    assert runner.advance(
        store, run_id, build_flow(fake_agent), ai_settings
    ) is RunState.AWAITING_EXPERT
    question = callback.pending(store, run_id)[0]
    calls_before_split = len(calls)
    callback.answer(store, question.id, "fragment the task", who="user")

    state = runner.advance(store, run_id, build_flow(fake_agent), ai_settings)

    assert state is RunState.COMPLETE
    assert len(store.latest(run_id, "decision")["schedule"]["blocks"]) == 2
    assert len(calls) == calls_before_split

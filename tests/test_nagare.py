"""Focused tests for Nagare's deterministic scheduling core."""
from datetime import datetime

import pytest
from pydantic import ValidationError

from demo.nagare.planner import baseline_schedule, candidate_slots, reschedule_task
from demo.nagare.schema import (
    CircadianProfile,
    ScheduleBlock,
    Task,
    TimeWindow,
    UserScheduleProfile,
)
from demo.nagare.validator import validate_schedule


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


def test_planner_does_not_use_protected_time():
    user_profile = profile(window(9, 17))
    user_profile.protected_blocks = [window(10, 12)]

    slots = candidate_slots(task(duration=120), 120, user_profile, [])

    assert slots
    assert all(not (start < DAY.replace(hour=12) and end > DAY.replace(hour=10))
               for start, end in slots)


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
    assert moved.start == DAY.replace(hour=9)
    assert moved.end == DAY.replace(hour=11)
    assert moved.id == "block_ml"


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

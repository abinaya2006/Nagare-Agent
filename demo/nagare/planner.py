"""Deterministic baseline scheduling and rescheduling helpers."""
from __future__ import annotations

from datetime import datetime, timedelta

from .schema import ProposedSchedule, ScheduleBlock, Task, UserScheduleProfile


def _overlaps(start: datetime, end: datetime, block: ScheduleBlock) -> bool:
    return start < block.end and end > block.start


def _protected(start: datetime, end: datetime, profile: UserScheduleProfile) -> bool:
    return any(start < window.end and end > window.start
               for window in [*profile.protected_blocks, profile.sleep_window])


def _fits(task: Task, start: datetime, end: datetime, profile: UserScheduleProfile,
          blocks: list[ScheduleBlock]) -> bool:
    if task.earliest_start and start < task.earliest_start:
        return False
    if task.latest_finish and end > task.latest_finish:
        return False
    if task.deadline and end > task.deadline:
        return False
    if not any(start >= window.start and end <= window.end
               for window in profile.available_windows):
        return False
    if _protected(start, end, profile):
        return False
    return not any(_overlaps(start, end, block) for block in blocks)


def candidate_slots(task: Task, duration: int, profile: UserScheduleProfile,
                    blocks: list[ScheduleBlock]) -> list[tuple[datetime, datetime]]:
    """Return feasible slots in chronological order, preferring user windows."""
    slots: list[tuple[datetime, datetime]] = []
    step = timedelta(minutes=profile.preferred_break_length)
    length = timedelta(minutes=duration)
    for window in profile.available_windows:
        cursor = window.start
        while cursor + length <= window.end:
            end = cursor + length
            if _fits(task, cursor, end, profile, blocks):
                slots.append((cursor, end))
            cursor += step
    preferred = profile.preferred_work_periods
    slots.sort(key=lambda slot: (not any(slot[0] >= p.start and slot[1] <= p.end for p in preferred),
                                 slot[0]))
    return slots


def baseline_schedule(tasks: list[Task], profile: UserScheduleProfile) -> ProposedSchedule:
    """Place tasks by fixedness, deadline, priority, and consequence."""
    blocks: list[ScheduleBlock] = []
    for task in sorted(tasks, key=lambda item: (
        not item.fixed, item.deadline or datetime.max, -item.priority,
        -item.consequence_of_delay, item.id)):
        slots = candidate_slots(task, task.estimated_duration, profile, blocks)
        if slots:
            start, end = slots[0]
            blocks.append(ScheduleBlock(id=f"block_{task.id}", task_id=task.id,
                                        start=start, end=end, block_type="task",
                                        locked=task.fixed or not task.movable))
    return ProposedSchedule(blocks=sorted(blocks, key=lambda block: block.start),
                            reasoning="Built from the user's constraints and preferences.")


def reschedule_task(task: Task, remaining_minutes: int, existing: list[ScheduleBlock],
                    tasks: list[Task], profile: UserScheduleProfile,
                    excluded_block_id: str | None = None) -> ProposedSchedule:
    """Move one missed task without changing protected or locked blocks."""
    kept = [block for block in existing if block.id != excluded_block_id]
    for start, end in candidate_slots(task, remaining_minutes, profile, kept):
        moved = kept + [ScheduleBlock(id=f"block_{task.id}", task_id=task.id,
                                      start=start, end=end, block_type="task")]
        return ProposedSchedule(blocks=sorted(moved, key=lambda block: block.start),
                                reasoning=f"Moved {task.title} into the earliest feasible user window.")
    return ProposedSchedule(blocks=kept,
                            reasoning=f"No feasible slot found for {task.title}.")
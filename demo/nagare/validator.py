"""Independent schedule validation (the planner never certifies itself)."""
from __future__ import annotations

from .planner import _overlaps, _protected
from .schema import (Conflict, ProposedSchedule, ScheduleValidationResult, Task,
                     UserScheduleProfile)


def validate_schedule(schedule: ProposedSchedule, tasks: list[Task],
                      profile: UserScheduleProfile, locked_blocks: list) -> ScheduleValidationResult:
    conflicts: list[Conflict] = []
    by_id = {task.id: task for task in tasks}
    for index, block in enumerate(schedule.blocks):
        task = by_id.get(block.task_id or "")
        if task is None or block.block_type != "task":
            continue
        if _protected(block.start, block.end, profile):
            conflicts.append(Conflict(task_id=task.id, conflict_type="protected_block", severity=5,
                                      resolvable=False, explanation="Task overlaps protected time."))
        if task.deadline and block.end > task.deadline:
            conflicts.append(Conflict(task_id=task.id, conflict_type="deadline", severity=5,
                                      resolvable=False, explanation="Task ends after its deadline."))
        for other in schedule.blocks[index + 1:]:
            if _overlaps(block.start, block.end, other):
                conflicts.append(Conflict(task_id=task.id, conflicting_block_id=other.id,
                                          conflict_type="time_overlap", severity=5,
                                          explanation="Task blocks overlap."))
        if not any(block.start >= window.start and block.end <= window.end
                   for window in profile.available_windows):
            conflicts.append(Conflict(task_id=task.id, conflict_type="availability", severity=4,
                                      explanation="Task is outside the user's availability."))
    current = {block.id: block for block in schedule.blocks}
    for locked in locked_blocks:
        candidate = current.get(locked.id)
        if candidate is None or candidate.start != locked.start or candidate.end != locked.end:
            conflicts.append(Conflict(conflicting_block_id=locked.id, conflict_type="locked_block",
                                      severity=5, resolvable=False,
                                      explanation="A locked block was changed or removed."))
    return ScheduleValidationResult(status="BLOCK" if conflicts else "PASS", conflicts=conflicts)
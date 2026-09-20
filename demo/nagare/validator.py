from datetime import timedelta

from .planner import _overlaps, _protected, candidate_slots
from .schema import (
    Conflict,
    ScheduleValidationResult,
)


def validate_schedule(
    schedule,
    tasks,
    profile,
    locked_blocks,
):
    conflicts = []

    def available_minutes_before_deadline(task):
        if task.deadline is None:
            return 0
        occupied_blocks = [
            block for block in schedule.blocks
            if block.task_id != task.id
        ] + list(locked_blocks)
        total = 0
        for window in profile.available_windows:
            end = min(window.end, task.deadline)
            if end <= window.start:
                continue
            cursor = window.start
            while cursor < end:
                next_cursor = min(cursor + timedelta(minutes=15), end)
                if not _protected(cursor, next_cursor, profile) and not any(
                    _overlaps(cursor, next_cursor, block)
                    for block in occupied_blocks
                ):
                    total += int((next_cursor - cursor).total_seconds() // 60)
                cursor = next_cursor
        return total

    by_id = {
        task.id: task
        for task in tasks
    }

    scheduled_task_ids = {
        block.task_id
        for block in schedule.blocks
        if block.block_type == "task" and block.task_id is not None
    }

    scheduled_minutes = {}
    for block in schedule.blocks:
        if block.block_type == "task" and block.task_id is not None:
            scheduled_minutes[block.task_id] = scheduled_minutes.get(block.task_id, 0) + int(
                (block.end - block.start).total_seconds() // 60
            )

    for task in tasks:
        if task.id in getattr(schedule, "deferred_task_ids", []):
            continue
        if task.id in scheduled_task_ids:
            remaining = task.estimated_duration - \
                scheduled_minutes.get(task.id, 0)
            accepted_pending = getattr(
                schedule, "pending_minutes", {}).get(task.id, 0)
            if remaining > 0 and accepted_pending != remaining:
                conflicts.append(
                    Conflict(
                        task_id=task.id,
                        conflict_type="duration",
                        severity=task.consequence_of_delay,
                        resolvable=True,
                        explanation=(
                            f"Task '{task.title}' has only "
                            f"{scheduled_minutes[task.id]} of {task.estimated_duration} "
                            f"minutes scheduled; {remaining} remain pending."
                        ),
                    )
                )
            continue

        conflict_type = "unresolved_constraint"
        explanation = (
            f"Task '{task.title}' could not be placed in the "
            "available time without violating a constraint."
        )
        available_minutes = available_minutes_before_deadline(task)
        occupied_blocks = [
            block for block in schedule.blocks
            if block.task_id != task.id
        ] + list(locked_blocks)
        no_single_slot = bool(task.deadline) and not candidate_slots(
            task, task.estimated_duration, profile, occupied_blocks)
        if task.deadline and 0 < available_minutes < task.estimated_duration:
            conflict_type = "fragmentation"
            explanation = (
                f"Task '{task.title}' has {available_minutes} usable minutes "
                f"before its {task.deadline.strftime('%H:%M')} deadline, but "
                f"needs {task.estimated_duration} minutes. Ask whether to "
                "split it into smaller sessions."
            )
        elif task.deadline and no_single_slot and available_minutes >= task.estimated_duration:
            conflict_type = "fragmentation"
            explanation = (
                f"Task '{task.title}' has enough total time before its "
                f"{task.deadline.strftime('%H:%M')} deadline, but no single "
                "uninterrupted slot. Ask whether to split it around the "
                "protected time."
            )

        conflicts.append(
            Conflict(
                task_id=task.id,
                conflict_type=conflict_type,
                severity=task.consequence_of_delay,
                resolvable=True,
                explanation=explanation,
            )
        )

    # --------------------------------------------------
    # 1. Validate every scheduled task
    # --------------------------------------------------

    for index, block in enumerate(schedule.blocks):

        if block.block_type != "task":
            continue

        task = by_id.get(block.task_id)

        if task is None:
            continue

        # Protected time
        if _protected(
            block.start,
            block.end,
            profile,
        ):
            conflicts.append(
                Conflict(
                    task_id=task.id,
                    conflicting_block_id=block.task_id,
                    conflict_type="protected_block",
                    severity=5,
                    resolvable=False,
                    explanation=(
                        f"Task '{task.title}' overlaps "
                        "a protected or sleep period."
                    ),
                )
            )

        # Deadline
        if task.deadline and block.end > task.deadline:
            conflicts.append(
                Conflict(
                    task_id=task.id,
                    conflicting_block_id=block.task_id,
                    conflict_type="deadline",
                    severity=5,
                    resolvable=False,
                    explanation=(
                        f"Task '{task.title}' finishes "
                        "after its deadline."
                    ),
                )
            )

        # Earliest start
        if (
            task.earliest_start
            and block.start < task.earliest_start
        ):
            conflicts.append(
                Conflict(
                    task_id=task.id,
                    conflicting_block_id=block.task_id,
                    conflict_type="availability",
                    severity=4,
                    resolvable=True,
                    explanation=(
                        f"Task '{task.title}' starts "
                        "before its allowed start time."
                    ),
                )
            )

        # Latest finish
        if (
            task.latest_finish
            and block.end > task.latest_finish
        ):
            conflicts.append(
                Conflict(
                    task_id=task.id,
                    conflicting_block_id=block.task_id,
                    conflict_type="availability",
                    severity=4,
                    resolvable=True,
                    explanation=(
                        f"Task '{task.title}' finishes "
                        "after its allowed finish time."
                    ),
                )
            )

        # Available windows
        inside_available_window = False

        for window in profile.available_windows:
            if (
                block.start >= window.start
                and block.end <= window.end
            ):
                inside_available_window = True
                break

        if not inside_available_window:
            conflicts.append(
                Conflict(
                    task_id=task.id,
                    conflicting_block_id=block.task_id,
                    conflict_type="availability",
                    severity=4,
                    resolvable=True,
                    explanation=(
                        f"Task '{task.title}' is outside "
                        "the user's available windows."
                    ),
                )
            )

        # --------------------------------------------------
        # 2. Check overlap with other blocks
        # --------------------------------------------------

        for other in schedule.blocks[index + 1:]:

            if not _overlaps(
                block.start,
                block.end,
                other,
            ):
                continue

            if block.task_id == other.task_id:
                continue

            conflicts.append(
                Conflict(
                    task_id=task.id,
                    conflicting_block_id=other.task_id,
                    conflict_type="time_overlap",
                    severity=5,
                    resolvable=True,
                    explanation=(
                        f"Task '{task.title}' overlaps "
                        f"another scheduled block."
                    ),
                )
            )

    # --------------------------------------------------
    # 3. Check locked blocks
    # --------------------------------------------------

    for locked in locked_blocks:

        matching_block = None

        for block in schedule.blocks:
            if block.id == locked.id:
                matching_block = block
                break

            if (
                matching_block is None
                and block.task_id == locked.task_id
                and block.task_id is not None
            ):
                matching_block = block

        # Locked block disappeared
        if matching_block is None:
            conflicts.append(
                Conflict(
                    task_id=locked.task_id,
                    conflicting_block_id=locked.id,
                    conflict_type="locked_block",
                    severity=5,
                    resolvable=False,
                    explanation=(
                        "A locked block was removed "
                        "from the schedule."
                    ),
                )
            )

            continue

        # Locked block moved
        if (
            matching_block.start != locked.start
            or matching_block.end != locked.end
        ):
            conflicts.append(
                Conflict(
                    task_id=locked.task_id,
                    conflicting_block_id=locked.id,
                    conflict_type="locked_block",
                    severity=5,
                    resolvable=False,
                    explanation=(
                        "A locked block was changed or moved."
                    ),
                )
            )

    # --------------------------------------------------
    # 4. Return validation result
    # --------------------------------------------------

    if conflicts:
        status = "BLOCK"
    else:
        status = "PASS"

    return ScheduleValidationResult(
        status=status,
        conflicts=conflicts,
    )

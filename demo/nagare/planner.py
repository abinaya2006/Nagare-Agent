from datetime import timedelta

from .schema import (
    ProposedSchedule,
    ScheduleBlock,
)


def _overlaps(start, end, block):
    return start < block.end and end > block.start


def _protected(start, end, profile):
    protected_windows = [
        *profile.protected_blocks,
        profile.sleep_window,
    ]

    for window in protected_windows:
        if start < window.end and end > window.start:
            return True

    return False


def _fits(task, start, end, profile, blocks):
    # Task's earliest allowed start
    if task.earliest_start and start < task.earliest_start:
        return False

    # Task's latest allowed finish
    if task.latest_finish and end > task.latest_finish:
        return False

    # Task deadline
    if task.deadline and end > task.deadline:
        return False

    # Must be inside an available window
    inside_available_window = False

    for window in profile.available_windows:
        if start >= window.start and end <= window.end:
            inside_available_window = True
            break

    if not inside_available_window:
        return False

    # Never place work over protected/sleep time
    if _protected(start, end, profile):
        return False

    # Never overlap another block
    for block in blocks:
        if _overlaps(start, end, block):
            return False

    return True


def candidate_slots(task, duration, profile, blocks):
    slots = []

    length = timedelta(minutes=duration)

    # Use preferred break length as the search step.
    # If it is invalid, use 10 minutes.
    step_minutes = profile.preferred_break_length

    if not step_minutes or step_minutes <= 0:
        step_minutes = 10

    step = timedelta(minutes=step_minutes)

    for window in profile.available_windows:
        cursor = window.start

        while cursor + length <= window.end:
            end = cursor + length

            if _fits(
                task,
                cursor,
                end,
                profile,
                blocks,
            ):
                slots.append((cursor, end))

            cursor += step

    # Preferred work periods first
    preferred = profile.preferred_work_periods

    def slot_key(slot):
        start, _ = slot

        preferred_match = False

        for period in preferred:
            if start >= period.start and start < period.end:
                preferred_match = True
                break

        return (
            not preferred_match,
            start,
        )

    slots.sort(key=slot_key)

    return slots


def _response_task_order(tasks, user_responses):
    """Return the order in which prior user answers mention undated tasks."""
    order = {}
    responses = user_responses or []
    for response in reversed(responses):
        answer = str(response).lower()
        for task in tasks:
            if task.id.lower() in answer or task.title.lower() in answer:
                order.setdefault(task.id, len(order))
    return order


def baseline_schedule(tasks, profile, user_responses=None, existing=None):
    blocks = list(existing or [])

    response_order = _response_task_order(tasks, user_responses)

    # Fixed tasks first, then deadline, priority, and user direction.
    ordered_tasks = sorted(
        tasks,
        key=lambda task: (
            not task.fixed,
            task.deadline is None,
            task.deadline.isoformat() if task.deadline else "9999-12-31T23:59:59",
            -task.priority,
            response_order.get(task.id, len(response_order)),
            -task.consequence_of_delay,
            task.id,
        ),
    )

    for task in ordered_tasks:

        slots = candidate_slots(
            task,
            task.estimated_duration,
            profile,
            blocks,
        )

        if not slots:
            continue

        start, end = slots[0]

        block = ScheduleBlock(
            id=f"block_{task.id}",
            task_id=task.id,
            start=start,
            end=end,
            block_type="task",
            locked=task.fixed or not task.movable,
        )

        blocks.append(block)

    blocks.sort(key=lambda block: block.start)

    return ProposedSchedule(
        blocks=blocks,
        conflicts=[],
        reschedule_attempts=[],
    )


def fragment_task(task, profile, existing=None):
    """Place a task across multiple feasible sessions before its deadline."""
    blocks = list(existing or [])
    remaining = task.estimated_duration
    fragment_length = task.preferred_fragment_duration or task.min_fragment_duration or 30
    fragment_length = max(fragment_length, task.min_fragment_duration or 1)
    fragment_number = 1

    while remaining > 0:
        duration = min(fragment_length, remaining)
        slots = candidate_slots(task, duration, profile, blocks)
        while not slots and duration > (task.min_fragment_duration or 1):
            duration -= task.min_fragment_duration or 1
            slots = candidate_slots(task, duration, profile, blocks)
        if not slots:
            break
        start, end = slots[0]
        blocks.append(ScheduleBlock(
            id=f"block_{task.id}_{fragment_number}",
            task_id=task.id,
            start=start,
            end=end,
            block_type="task",
            locked=task.fixed or not task.movable,
        ))
        remaining -= duration
        fragment_number += 1

    blocks.sort(key=lambda block: block.start)
    return ProposedSchedule(
        blocks=blocks,
        pending_minutes={task.id: remaining} if remaining else {},
        conflicts=[],
        reschedule_attempts=[],
    )


def reschedule_task(
    task,
    remaining_minutes,
    existing,
    tasks,
    profile,
    excluded_block_id=None,
):
    """
    Try to place the missed task into a feasible slot.

    Existing blocks are treated as occupied.
    Protected/fixed blocks are never moved.

    This version does not modify existing blocks.
    It returns a schedule containing the existing blocks
    plus the newly placed task if a slot is found.
    """

    kept = []
    removed = []

    for block in existing:
        # The current schema may not have block.id.
        # Therefore we use task_id when identifying the
        # block that belongs to the missed task.
        if excluded_block_id is not None and (
            block.id == excluded_block_id or block.task_id == excluded_block_id
        ):
            removed.append(block)
            continue

        kept.append(block)

    slots = candidate_slots(
        task,
        remaining_minutes,
        profile,
        kept,
    )

    if removed:
        previous_end = max(block.end for block in removed)
        future_slots = [
            slot for slot in slots
            if slot[0] >= previous_end
        ]
        different_slots = [
            slot for slot in slots
            if all(slot != (block.start, block.end) for block in removed)
        ]
        slots = future_slots or different_slots

    if not slots:
        return ProposedSchedule(
            blocks=kept,
            conflicts=[],
            reschedule_attempts=[],
        )

    start, end = slots[0]

    new_block = ScheduleBlock(
        id=f"block_{task.id}",
        task_id=task.id,
        start=start,
        end=end,
        block_type="task",
        locked=task.fixed or not task.movable,
    )

    kept.append(new_block)

    kept.sort(key=lambda block: block.start)

    return ProposedSchedule(
        blocks=kept,
        conflicts=[],
        reschedule_attempts=[],
    )

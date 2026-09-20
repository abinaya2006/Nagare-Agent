"""Nagare's agentic scheduling and rescheduling flow.

The AI model drafts the initial schedule and reschedules. The validator checks it,
and the runner supplies the bounded back-edge for revisions and escalation.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from slice import callback
from slice.llm import ModelError
from slice.records import RunState

from .planner import _protected, baseline_schedule, fragment_task, reschedule_task
from .schema import (
    ProposedSchedule,
    RescheduleAttempt,
    ScheduleBlock,
    ScheduleValidationResult,
    Task,
    UserScheduleProfile,
)
from .validator import validate_schedule


MAX_REVISIONS = 3


def build_draft_messages(context: dict, prior: dict | None,
                         validation: dict | None, answer: dict | None) -> list[dict]:
    """Build a compact, inspectable request for the schedule planner agent."""
    return [
        {"role": "system", "content": (
            "You are Nagare's agentic scheduling planner. Your job is to output a ProposedSchedule JSON.\n"
            "RULES:\n"
            "1. Fit all tasks into the available_windows. Do NOT overlap tasks.\n"
            "2. Preserve all locked/protected blocks exactly as they are.\n"
            "3. Treat hard deadlines as feasibility constraints first. Among tasks that can be safely placed, "
            "prefer higher priority, then the earliest deadline. For undated ties, use the user's prior answers.\n"
            "4. If an 'expert_answer' is provided, follow the user's instructions EXACTLY. "
            "(e.g., if they say 'move it to tomorrow', place it in tomorrow's available window. "
            "If they say 'split it', output two ScheduleBlocks for the same task_id).\n"
            "5. Keep the reasoning string under 160 characters."
        )},
        {"role": "user", "content": json.dumps(
            {"context": context, "previous_schedule": prior,
             "validation": validation, "expert_answer": answer},
            indent=2,
            default=str,
        )},
    ]


def build_flow(call=None):
    """Return Nagare's bounded agentic flow."""

    def _input(ctx) -> dict:
        return ctx.latest("input") or {}

    def _models(ctx) -> tuple[list[Task], UserScheduleProfile, list[ScheduleBlock]]:
        payload = _input(ctx)
        tasks = [Task.model_validate(item)
                 for item in payload.get("tasks", [])]
        profile = UserScheduleProfile.model_validate(payload["profile"])
        existing = [
            ScheduleBlock.model_validate(item)
            for item in payload.get("existing_blocks", [])
        ]
        return tasks, profile, existing

    def _conflict_signature(conflict: dict) -> tuple:
        if not isinstance(conflict, dict):
            conflict = conflict.model_dump()
        return (
            conflict.get("task_id"),
            conflict.get("conflicting_block_id"),
            conflict.get("conflict_type"),
        )

    def _should_ask(ctx, validation: ScheduleValidationResult) -> bool:
        history = ctx.history("validation")
        # If the agent is stuck repeating the exact same error, ask the user.
        if len(history) >= 2:
            previous = {
                _conflict_signature(item)
                for item in history[-2].payload.get("conflicts", [])
            }
            current = {
                _conflict_signature(item)
                for item in validation.conflicts
            }
            if previous.intersection(current):
                return True
        # Or if it exceeds max revisions
        return len(ctx.history("reschedule_attempt")) >= MAX_REVISIONS

    def _ask_user(ctx, validation: ScheduleValidationResult) -> RunState:
        tasks, profile, _ = _models(ctx)
        task_by_id = {task.id: task for task in tasks}
        has_fragmentation = any(
            conflict.conflict_type == "fragmentation"
            for conflict in validation.conflicts
        )
        candidates = [
            task_by_id[conflict.task_id]
            for conflict in validation.conflicts
            if conflict.task_id in task_by_id
            and conflict.conflict_type in {"fragmentation", "duration", "unresolved_constraint"}
        ]
        candidates.sort(key=lambda task: (
            task.deadline is None,
            task.deadline.isoformat() if task.deadline else "9999-12-31T23:59:59",
            -task.priority,
        ))
        recommended = candidates[0] if candidates else None
        diagnosis = "Nagare found a scheduling conflict."
        available_minutes = 0
        for window in profile.available_windows:
            cursor = window.start
            while cursor < window.end:
                next_cursor = min(cursor + timedelta(minutes=15), window.end)
                if not _protected(cursor, next_cursor, profile):
                    available_minutes += int(
                        (next_cursor - cursor).total_seconds() // 60
                    )
                cursor = next_cursor
        requested_minutes = sum(task.estimated_duration for task in tasks)
        overload = max(0, requested_minutes - available_minutes)
        if overload:
            diagnosis = f"Nagare found a scheduling conflict: your day is overloaded by {overload} minutes."
        if recommended is not None:
            matching = next(
                conflict for conflict in validation.conflicts
                if conflict.task_id == recommended.id
            )
            diagnosis = (
                f"{diagnosis} {recommended.title} is at risk: {matching.explanation} "
                "I recommend protecting this task first."
            )
            if recommended.deadline and profile.available_windows:
                available_until = max(
                    window.end for window in profile.available_windows
                )
                diagnosis += (
                    f" The day remains available until {available_until.strftime('%H:%M')}, "
                    f"but {recommended.title} must be completed by "
                    f"{recommended.deadline.strftime('%H:%M')}; time after that deadline "
                    "cannot repair this task."
                )
        if has_fragmentation:
            question = (
                f"{diagnosis} Options: 1) split or fragment {recommended.title if recommended else 'the task'} "
                "2) move it to tomorrow 3) rebalance a lower-priority task. What should I do?"
            )
        else:
            question = (
                f"{diagnosis} Options: 1) move the task to tomorrow 2) shorten it "
                "3) rebalance a lower-priority task. What should I do?"
            )
        callback.ask(
            ctx.store,
            ctx.run_id,
            question,
            {
                "conflicts": [c.model_dump(mode="json") for c in validation.conflicts],
                "diagnosis": diagnosis,
                "recommended_task_id": recommended.id if recommended else None,
                "resume_state": RunState.DRAFTING.value,
            },
            ctx.settings,
        )
        return RunState.AWAITING_EXPERT

    def handle_drafting(ctx) -> RunState:
        payload = _input(ctx)
        answer = ctx.latest("expert_answer")
        answer_text = answer.get(
            "answer", "").strip().lower() if answer else ""

        # Abort conditions
        if answer and not answer_text:
            ctx.append("failure", {"kind": "no_user_decision",
                       "detail": "The scheduling question was not answered."}, produced_by="system")
            return RunState.FAILED

        if answer_text and any(phrase in answer_text for phrase in ("leave it missed", "leave missed", "do not move")):
            ctx.append("failure", {
                       "kind": "user_declined", "detail": "The user chose to leave the task unresolved."}, produced_by="system")
            return RunState.FAILED

        tasks, profile, existing = _models(ctx)
        missed_task_id = payload.get(
            "missed_task_id") or payload.get("block_id")

        # Explicit human actions are authoritative in both AI and offline mode.
        # Do not ask the model to reinterpret a button the user already chose.
        if answer_text and ("fragment" in answer_text or "split" in answer_text):
            conflict_ids = {
                item.get("task_id")
                for item in (ctx.latest("validation") or {}).get("conflicts", [])
                if item.get("conflict_type") == "fragmentation"
            }
            selected_task_id = None
            if answer:
                question = ctx.store.get_question(answer.get("question_id"))
                selected_task_id = (question.context or {}).get(
                    "recommended_task_id") if question else None
            tasks_to_fragment = [
                item for item in tasks
                if item.id in conflict_ids
                and (selected_task_id is None or item.id == selected_task_id)
            ]
            if tasks_to_fragment:
                task_to_fragment = tasks_to_fragment[0]
                fragment = fragment_task(task_to_fragment, profile, existing)
                remaining_tasks = [
                    task for task in tasks if task.id != task_to_fragment.id
                ]
                replanned = baseline_schedule(
                    remaining_tasks,
                    profile,
                    user_responses=[
                        item.payload.get("answer", "")
                        for item in ctx.history("expert_answer")
                    ],
                    existing=fragment.blocks,
                )
                split_schedule = replanned.model_copy(update={
                    "pending_minutes": fragment.pending_minutes,
                })
                ctx.append(
                    "proposed_schedule",
                    split_schedule.model_dump(mode="json"),
                    produced_by="planner:human_fragmentation",
                )
                return RunState.GATING

        if answer_text and ("rebalanc" in answer_text or "rebanc" in answer_text):
            selected_task_id = None
            if answer:
                question = ctx.store.get_question(answer.get("question_id"))
                selected_task_id = (question.context or {}).get(
                    "recommended_task_id") if question else None
            protected_task = next(
                (task for task in tasks if task.id == selected_task_id), None)
            lower_priority = [
                task for task in tasks
                if task.id != selected_task_id
                and (protected_task is None or task.priority < protected_task.priority)
            ]
            lower_priority.sort(key=lambda task: (
                task.priority,
                task.deadline is not None,
                task.deadline.isoformat() if task.deadline else "9999-12-31T23:59:59",
            ))
            if lower_priority:
                deferred = lower_priority[0]
                replanned = baseline_schedule(
                    [task for task in tasks if task.id != deferred.id],
                    profile,
                    user_responses=[
                        item.payload.get("answer", "")
                        for item in ctx.history("expert_answer")
                    ],
                    existing=existing,
                )
                rebalanced = replanned.model_copy(update={
                    "pending_minutes": {
                        **replanned.pending_minutes,
                        deferred.id: deferred.estimated_duration,
                    },
                    "deferred_task_ids": [deferred.id],
                })
                ctx.append(
                    "proposed_schedule",
                    rebalanced.model_dump(mode="json"),
                    produced_by="planner:human_rebalance",
                )
                return RunState.GATING

        if answer_text and not call:
            if "shorten" in answer_text:
                import re
                duration_match = re.search(
                    r"\b(?:to|for)\s+(\d+)\s*(?:minutes?|mins?)?", answer_text)
                if duration_match:
                    shortened_duration = int(duration_match.group(1))
                    conflict = next(
                        (
                            item for item in (ctx.latest("validation") or {}).get("conflicts", [])
                            if item.get("task_id")
                        ),
                        None,
                    )
                    task_id = conflict.get(
                        "task_id") if conflict else missed_task_id
                    tasks = [
                        item.model_copy(update={
                            "estimated_duration": shortened_duration
                        }) if item.id == task_id else item
                        for item in tasks
                    ]
                    payload = dict(payload)
                    payload["tasks"] = [item.model_dump(
                        mode="json") for item in tasks]
                    ctx.append("input", payload, produced_by="user_decision")

            if "tomorrow" in answer_text and "task" in answer_text:
                schedule = ctx.latest("proposed_schedule") or {"blocks": []}
                validation = ctx.latest("validation") or {
                    "status": "BLOCK", "conflicts": []}
                ctx.append(
                    "decision",
                    {
                        "status": "scheduled",
                        "schedule": schedule,
                        "validation": validation,
                        "explanation": "The user authorized deferring the conflicting task to tomorrow.",
                    },
                    produced_by="user_decision",
                )
                return RunState.COMPLETE

        context = {
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "profile": profile.model_dump(mode="json"),
            "existing_blocks": [block.model_dump(mode="json") for block in existing],
            "missed_task_id": missed_task_id,
            "reason": payload.get("reason"),
            "prior_user_responses": [
                item.payload.get("answer", "")
                for item in ctx.history("expert_answer")
            ],
        }

        prior = ctx.latest("proposed_schedule")
        validation = ctx.latest("validation")

        def offline_schedule() -> ProposedSchedule:
            missed_task = next(
                (t for t in tasks if t.id == missed_task_id), None)
            if missed_task is not None:
                remaining_minutes = missed_task.estimated_duration
                return reschedule_task(
                    missed_task,
                    remaining_minutes,
                    existing,
                    tasks,
                    profile,
                    excluded_block_id=missed_task.id,
                )
            return baseline_schedule(
                tasks,
                profile,
                user_responses=[
                    item.payload.get("answer", "")
                    for item in ctx.history("expert_answer")
                ],
            )

        used_agent = call is not None
        if used_agent:
            try:
                schedule = call(
                    settings=ctx.settings,
                    budget=ctx.budget,
                    messages=build_draft_messages(
                        context, prior, validation, answer),
                    schema=ProposedSchedule,
                    step="nagare_draft",
                )
            except ModelError as exc:
                ctx.append("agent_fallback", {"kind": type(
                    exc).__name__, "detail": str(exc)}, produced_by="system")
                schedule = offline_schedule()
                used_agent = False
        else:
            schedule = offline_schedule()

        if used_agent:
            fallback_validation = validate_schedule(
                offline_schedule(),
                tasks,
                profile,
                [block for block in existing if block.locked],
            )
            proposed_task_ids = {
                block.task_id
                for block in schedule.blocks
                if block.block_type == "task" and block.task_id is not None
            }
            fallback_task_ids = {
                block.task_id
                for block in offline_schedule().blocks
                if block.block_type == "task" and block.task_id is not None
            }
            if (
                fallback_validation.status == "PASS"
                and fallback_task_ids - proposed_task_ids
            ):
                ctx.append(
                    "agent_fallback",
                    {
                        "kind": "incomplete_proposal",
                        "detail": (
                            "The AI omitted tasks that fit the supplied deadline "
                            "and availability; used the validated fallback schedule."
                        ),
                    },
                    produced_by="system",
                )
                schedule = offline_schedule()
                used_agent = False

        ctx.append(
            "proposed_schedule",
            schedule.model_dump(mode="json"),
            produced_by="agent:nagare_draft" if used_agent else "planner:offline",
        )
        ctx.append(
            "reschedule_attempt",
            RescheduleAttempt(
                attempt_number=len(ctx.history("reschedule_attempt")) + 1,
                reason="AI generated schedule based on context and feedback." if used_agent else "Offline fallback.",
                changes=["Generated schedule."],
                conflicts_found=[],
                outcome="unresolved",
            ).model_dump(mode="json"),
            produced_by="agent:nagare_draft" if used_agent else "planner:offline",
        )
        return RunState.GATING

    def handle_gating(ctx) -> RunState:
        proposed = ctx.latest("proposed_schedule")
        if proposed is None:
            ctx.append("failure", {"kind": "missing_schedule",
                       "detail": "Validation started without a proposed schedule."}, produced_by="system")
            return RunState.FAILED

        tasks, profile, existing = _models(ctx)
        schedule = ProposedSchedule.model_validate(proposed)
        locked_blocks = [block for block in existing if block.locked]

        # Rule-based validation ensures the AI didn't hallucinate!
        validation = validate_schedule(schedule, tasks, profile, locked_blocks)

        ctx.append(
            "validation",
            validation.model_dump(mode="json"),
            produced_by="validator:rule_based",
        )

        if validation.status == "PASS":
            ctx.append(
                "decision",
                {
                    "status": "scheduled",
                    "schedule": schedule.model_dump(mode="json"),
                    "validation": validation.model_dump(mode="json"),
                    "explanation": "Schedule passed all mathematical checks.",
                },
                produced_by="system",
            )
            return RunState.COMPLETE

        if _should_ask(ctx, validation):
            return _ask_user(ctx, validation)

        return RunState.DRAFTING

    return SimpleNamespace(
        name="nagare",
        handlers={
            RunState.DRAFTING: handle_drafting,
            RunState.GATING: handle_gating,
        },
    )

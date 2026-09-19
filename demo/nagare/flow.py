"""Nagare's rule-based scheduling and rescheduling flow.

The planner proposes a schedule and the validator checks it. The runner's
state machine supplies the agentic back-edge when validation finds a conflict.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from slice import callback
from slice.records import RunState

from .schema import (
    ProposedSchedule,
    RescheduleAttempt,
    ScheduleValidationResult,
    ScheduleBlock,
    Task,
    UserScheduleProfile,
)
from .planner import baseline_schedule, reschedule_task
from .validator import validate_schedule


MAX_REVISIONS = 2


def build_draft_messages(context: dict, prior: dict | None,
                         validation: dict | None, answer: dict | None) -> list[dict]:
    """Build an inspectable planner request for future model integration."""
    return [
        {"role": "system", "content": "Build a schedule from the supplied constraints."},
        {"role": "user", "content": json.dumps(
            {"context": context, "previous_schedule": prior,
             "validation": validation, "answer": answer},
            indent=2,
            default=str,
        )},
    ]


def build_check_messages(context: dict, schedule: dict) -> list[dict]:
    return [
        {"role": "system", "content": "Check the proposed schedule against all constraints."},
        {"role": "user", "content": json.dumps(
            {"context": context, "schedule": schedule},
            indent=2,
            default=str,
        )},
    ]


def build_flow(call=None):
    """Return a deterministic Nagare Flow.

    ``call`` remains accepted for compatibility with the smoke-flow shape, but
    rule-based planning and validation do not make model calls.
    """

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
        return (
            conflict.get("task_id"),
            conflict.get("conflicting_block_id"),
            conflict.get("conflict_type"),
        )

    def _should_ask(ctx, validation: ScheduleValidationResult) -> bool:
        history = ctx.history("validation")
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
        return len(ctx.history("reschedule_attempt")) >= MAX_REVISIONS

    def _ask_user(ctx, validation: ScheduleValidationResult) -> RunState:
        question = (
            "No safe slot was found today. Choose one: (1) shorten the missed "
            "task, (2) move the blocking task to tomorrow, or (3) move the "
            "missed task to tomorrow."
        )
        callback.ask(
            ctx.store,
            ctx.run_id,
            question,
            {
                "conflicts": [
                    conflict.model_dump(mode="json")
                    for conflict in validation.conflicts
                ],
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
        if answer and not answer_text:
            ctx.append(
                "failure",
                {"kind": "no_user_decision",
                    "detail": "The scheduling question was not answered."},
                produced_by="system",
            )
            return RunState.FAILED
        if answer_text and any(
            phrase in answer_text
            for phrase in ("leave it missed", "leave missed", "do not move", "don't move")
        ):
            ctx.append(
                "failure",
                {"kind": "user_declined",
                    "detail": "The user chose to leave the task unresolved."},
                produced_by="system",
            )
            return RunState.FAILED

        tasks, profile, existing = _models(ctx)
        missed_task_id = payload.get(
            "missed_task_id") or payload.get("block_id")
        missed_task = next(
            (task for task in tasks if task.id == missed_task_id), None)
        if missed_task is not None:
            schedule = reschedule_task(
                missed_task,
                missed_task.estimated_duration,
                existing,
                tasks,
                profile,
            )
        else:
            schedule = baseline_schedule(tasks, profile)

        ctx.append(
            "proposed_schedule",
            schedule.model_dump(mode="json"),
            produced_by="planner:rule_based",
        )
        ctx.append(
            "reschedule_attempt",
            RescheduleAttempt(
                attempt_number=len(ctx.history("reschedule_attempt")) + 1,
                reason="Place tasks by deadline, priority, and available windows.",
                changes=[
                    "Generated a deterministic schedule from current constraints."],
                conflicts_found=[],
                outcome="unresolved",
            ).model_dump(mode="json"),
            produced_by="planner:rule_based",
        )
        return RunState.GATING

    def handle_gating(ctx) -> RunState:
        proposed = ctx.latest("proposed_schedule")
        if proposed is None:
            ctx.append(
                "failure",
                {"kind": "missing_schedule",
                    "detail": "Validation started without a proposed schedule."},
                produced_by="system",
            )
            return RunState.FAILED

        tasks, profile, existing = _models(ctx)
        schedule = ProposedSchedule.model_validate(proposed)
        locked_blocks = [block for block in existing if block.locked]
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
                    "explanation": "Schedule passed all rule-based checks.",
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

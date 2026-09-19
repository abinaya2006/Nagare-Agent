"""Nagare's agentic scheduling and rescheduling flow.

An optional model planner proposes a typed schedule, the validator checks it,
and the runner supplies the bounded back-edge for revisions and escalation.
Without a model call, the deterministic planner remains available offline.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from slice import callback
from slice.llm import ModelError
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
    """Build a compact, inspectable request for the schedule planner agent."""
    return [
        {"role": "system", "content": (
            "You are Nagare's scheduling planner. Return only the requested JSON "
            "schedule. Use only feasible available windows, preserve locked blocks, "
            "schedule each task when possible, and keep reasoning under 160 characters."
        )},
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
    """Return Nagare's bounded agentic flow.

    ``call`` is injected for live model use and testing. Omitting it selects the
    offline deterministic planner; validation and escalation stay in code.
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
        if not isinstance(conflict, dict):
            conflict = conflict.model_dump()
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

        if answer_text and "tomorrow" in answer_text and "task" in answer_text:
            schedule = ctx.latest("proposed_schedule") or {"blocks": []}
            validation = ctx.latest("validation") or {
                "status": "BLOCK", "conflicts": []}
            ctx.append(
                "decision",
                {
                    "status": "scheduled",
                    "schedule": schedule,
                    "validation": validation,
                    "explanation": (
                        "The user authorized deferring the conflicting task "
                        "to tomorrow."
                    ),
                },
                produced_by="user_decision",
            )
            return RunState.COMPLETE

        tasks, profile, existing = _models(ctx)
        missed_task_id = payload.get(
            "missed_task_id") or payload.get("block_id")
        context = {
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "profile": profile.model_dump(mode="json"),
            "existing_blocks": [
                block.model_dump(mode="json") for block in existing
            ],
            "missed_task_id": missed_task_id,
            "reason": payload.get("reason"),
        }
        prior = ctx.latest("proposed_schedule")
        validation = ctx.latest("validation")

        def offline_schedule() -> ProposedSchedule:
            missed_task = next(
                (task for task in tasks if task.id == missed_task_id), None)
            if missed_task is not None:
                return reschedule_task(
                    missed_task,
                    missed_task.estimated_duration,
                    existing,
                    tasks,
                    profile,
                )
            return baseline_schedule(tasks, profile)

        used_agent = call is not None
        if used_agent:
            try:
                schedule = call(
                    settings=ctx.settings,
                    budget=ctx.budget,
                    messages=build_draft_messages(
                        context, prior, validation, answer,
                    ),
                    schema=ProposedSchedule,
                    step="nagare_draft",
                )
            except ModelError as exc:
                ctx.append(
                    "agent_fallback",
                    {"kind": type(exc).__name__, "detail": str(exc)},
                    produced_by="system",
                )
                schedule = offline_schedule()
                used_agent = False
        else:
            schedule = offline_schedule()

        if used_agent:
            feasible = offline_schedule()
            proposed_task_ids = {
                block.task_id
                for block in schedule.blocks
                if block.block_type == "task" and block.task_id is not None
            }
            feasible_task_ids = {
                block.task_id
                for block in feasible.blocks
                if block.block_type == "task" and block.task_id is not None
            }
            if feasible_task_ids - proposed_task_ids:
                ctx.append(
                    "agent_fallback",
                    {
                        "kind": "incomplete_proposal",
                        "detail": (
                            "The agent omitted tasks that fit the supplied "
                            "availability; used the feasible fallback schedule."
                        ),
                    },
                    produced_by="system",
                )
                schedule = feasible
                used_agent = False

        ctx.append(
            "proposed_schedule",
            schedule.model_dump(mode="json"),
            produced_by=(
                "agent:nagare_draft" if used_agent else "planner:offline"
            ),
        )
        ctx.append(
            "reschedule_attempt",
            RescheduleAttempt(
                attempt_number=len(ctx.history("reschedule_attempt")) + 1,
                reason=(
                    "Agent proposed a schedule from the supplied constraints."
                    if used_agent
                    else "Offline planner placed tasks by deadline, priority, and availability."
                ),
                changes=[
                    "Generated a deterministic schedule from current constraints."],
                conflicts_found=[],
                outcome="unresolved",
            ).model_dump(mode="json"),
            produced_by=(
                "agent:nagare_draft" if used_agent else "planner:offline"
            ),
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

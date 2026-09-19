"""Nagare's agentic scheduling and rescheduling flow.

The AI model drafts the initial schedule and reschedules. The validator checks it,
and the runner supplies the bounded back-edge for revisions and escalation.
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
            "3. If an 'expert_answer' is provided, follow the user's instructions EXACTLY. "
            "(e.g., if they say 'move it to tomorrow', place it in tomorrow's available window. "
            "If they say 'split it', output two ScheduleBlocks for the same task_id).\n"
            "4. Keep the reasoning string under 160 characters."
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
        has_fragmentation = any(
            conflict.conflict_type == "fragmentation"
            for conflict in validation.conflicts
        )
        question = (
            "A task cannot fit as one session before its deadline. Should I "
            "split it into smaller sessions, or move something else to tomorrow?"
            if has_fragmentation else
            "I couldn't find a safe slot without breaking rules. How should I resolve this? "
            "(e.g., 'Move task X to tomorrow', 'Shorten task Y to 30 mins', 'Split it up')"
        )
        callback.ask(
            ctx.store,
            ctx.run_id,
            question,
            {
                "conflicts": [c.model_dump(mode="json") for c in validation.conflicts],
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

        context = {
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "profile": profile.model_dump(mode="json"),
            "existing_blocks": [block.model_dump(mode="json") for block in existing],
            "missed_task_id": missed_task_id,
            "reason": payload.get("reason"),
        }

        prior = ctx.latest("proposed_schedule")
        validation = ctx.latest("validation")

        def offline_schedule() -> ProposedSchedule:
            missed_task = next(
                (t for t in tasks if t.id == missed_task_id), None)
            if missed_task is not None:
                return reschedule_task(missed_task, missed_task.estimated_duration, existing, tasks, profile)
            return baseline_schedule(tasks, profile)

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

        # NOTE: We removed the 'feasible fallback override'. The AI's schedule is now trusted
        # and sent to the Validator. The Validator will catch omissions!

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

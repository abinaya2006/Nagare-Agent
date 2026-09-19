"""Nagare Focus Agent: selected-task recommendations, checkpoints, and memory."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from slice import callback
from slice.llm import ModelError
from slice.records import RunState


MAX_REPLANS = 3


class FocusTask(BaseModel):
    id: str
    title: str
    description: str | None = None
    deadline: date | datetime | None = None
    estimated_minutes: int = Field(gt=0)
    priority: str | int = "medium"
    status: str = "pending"


class FocusRecommendation(BaseModel):
    task_id: str
    recommendation: str
    reason: str
    suggested_duration_minutes: int = Field(gt=0, le=120)


class BreakdownAction(BaseModel):
    parent_task_id: str
    step: str
    estimated_minutes: int = Field(gt=0, le=30)
    completion_condition: str


def build_focus_messages(task: FocusTask, history: list[dict[str, Any]], context: dict[str, Any]) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are Nagare Focus Agent. Recommend exactly the selected task, "
                "not a different task. Return only FocusRecommendation JSON. Keep the "
                "suggested block practical and concise; do not expose chain-of-thought."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"selected_task": task.model_dump(
                    mode="json"), "history": history, "context": context},
                default=str,
                indent=2,
            ),
        },
    ]


def build_breakdown_messages(task: FocusTask, reason: str, history: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are Nagare Focus Agent. Break the selected task into exactly one "
                "small, concrete next action. Return only BreakdownAction JSON. The "
                "action must take 5-30 minutes and have a clear completion condition."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"task": task.model_dump(
                    mode="json"), "rejection_reason": reason, "history": history},
                default=str,
                indent=2,
            ),
        },
    ]


def _priority_score(priority: str | int) -> int:
    if isinstance(priority, int):
        return max(1, min(priority, 5))
    return {"low": 1, "medium": 3, "high": 5, "urgent": 5}.get(priority.lower(), 3)


def _history_payload(store, domain: str = "nagare_focus") -> list[dict[str, Any]]:
    return [
        {"kind": version.kind, **version.payload}
        for version in store.domain_history(domain)
        if version.kind in {
            "focus_recommendation",
            "focus_decision",
            "rejection_event",
            "breakdown_action",
            "breakdown_decision",
            "focus_outcome",
        }
    ]


def _task_history(history: list[dict[str, Any]], task_id: str) -> list[dict[str, Any]]:
    return [item for item in history if item.get("task_id") == task_id or item.get("parent_task_id") == task_id]


def _memory_duration(task: FocusTask, history: list[dict[str, Any]]) -> int:
    task_history = _task_history(history, task.id)
    long_rejections = sum(
        1 for item in task_history
        if item.get("kind") == "rejection_event"
        and any(word in item.get("reason", "").lower() for word in ("long", "too much", "overwhelming"))
    )
    if long_rejections:
        return min(20, task.estimated_minutes)
    return min(30, task.estimated_minutes)


def _fallback_recommendation(task: FocusTask, history: list[dict[str, Any]], context: dict[str, Any]) -> FocusRecommendation:
    duration = _memory_duration(task, history)
    task_history = _task_history(history, task.id)
    breakdown_count = sum(1 for item in task_history if item.get(
        "kind") == "breakdown_action")
    if breakdown_count:
        reason = "A smaller next step keeps this task concrete after your earlier difficulty starting it."
    elif task.deadline:
        reason = f"It is due {task.deadline.strftime('%Y-%m-%d')} and this is a manageable first block."
    else:
        reason = "This is a short, focused block for the task you selected."
    return FocusRecommendation(
        task_id=task.id,
        recommendation=f"Work on {task.title} for {duration} minutes.",
        reason=reason,
        suggested_duration_minutes=duration,
    )


def _fallback_breakdown(task: FocusTask, reason: str) -> BreakdownAction:
    description = (task.description or "").strip().rstrip(".")
    step = (
        f"Write down the first concrete action for {task.title}"
        if not description
        else f"Read the task description and list the first concrete action for {task.title}"
    )
    return BreakdownAction(
        parent_task_id=task.id,
        step=step,
        estimated_minutes=10,
        completion_condition="You have one specific action written down and ready to start.",
    )


def _answer_context(ctx) -> dict[str, Any] | None:
    answer = ctx.latest("expert_answer")
    if not answer:
        return None
    question = ctx.store.get_question(answer.get("question_id"))
    return question.context if question else None


def _append_state(ctx, state: str, detail: str) -> None:
    ctx.append("focus_state", {"state": state,
               "detail": detail}, produced_by="nagare_focus")


def _ask(ctx, question: str, kind: str, context: dict[str, Any]) -> RunState:
    callback.ask(
        ctx.store,
        ctx.run_id,
        question,
        {"kind": kind, **context, "resume_state": RunState.DRAFTING.value},
        ctx.settings,
    )
    return RunState.AWAITING_EXPERT


def build_focus_flow(call=None):
    """Build the bounded focus loop using the shared runner and callback store."""

    def input_data(ctx) -> dict[str, Any]:
        return ctx.latest("input") or {}

    def load_task(ctx) -> tuple[FocusTask, list[FocusTask]]:
        payload = input_data(ctx)
        tasks = [FocusTask.model_validate(item)
                 for item in payload.get("tasks", [])]
        selected_id = payload.get("selected_task_id")
        selected = next(
            (item for item in tasks if item.id == selected_id), None)
        if selected is None:
            raise ValueError("selected_task_id must identify one pending task")
        return selected, tasks

    def recommend(ctx, task: FocusTask, history: list[dict[str, Any]]) -> FocusRecommendation:
        context = input_data(ctx).get("context", {})
        if call is not None:
            try:
                recommendation = call(
                    settings=ctx.settings,
                    budget=ctx.budget,
                    messages=build_focus_messages(
                        task, _task_history(history, task.id), context),
                    schema=FocusRecommendation,
                    step="nagare_focus_recommendation",
                )
                if recommendation.task_id == task.id:
                    return recommendation
                ctx.append(
                    "focus_fallback",
                    {"kind": "wrong_task",
                        "detail": "The model selected a task other than the user's selected task."},
                    produced_by="system",
                )
            except (ModelError, ValidationError) as exc:
                ctx.append("focus_fallback", {"kind": type(
                    exc).__name__, "detail": str(exc)}, produced_by="system")
        return _fallback_recommendation(task, history, context)

    def breakdown(ctx, task: FocusTask, reason: str, history: list[dict[str, Any]]) -> BreakdownAction:
        if call is not None:
            try:
                action = call(
                    settings=ctx.settings,
                    budget=ctx.budget,
                    messages=build_breakdown_messages(
                        task, reason, _task_history(history, task.id)),
                    schema=BreakdownAction,
                    step="nagare_focus_breakdown",
                )
                if action.parent_task_id == task.id:
                    return action
            except (ModelError, ValidationError) as exc:
                ctx.append("focus_fallback", {"kind": type(
                    exc).__name__, "detail": str(exc)}, produced_by="system")
        return _fallback_breakdown(task, reason)

    def handle_drafting(ctx) -> RunState:
        task, _ = load_task(ctx)
        history = _history_payload(ctx.store)
        answer_context = _answer_context(ctx)
        answer = ctx.latest("expert_answer")

        if not answer:
            recommendation = recommend(ctx, task, history)
            ctx.append("focus_recommendation", {**recommendation.model_dump(
                mode="json"), "task_id": task.id}, produced_by="agent:nagare_focus")
            _append_state(ctx, "waiting_for_human",
                          "Recommendation is awaiting a user decision.")
            return _ask(
                ctx,
                f"Nagare recommends: {recommendation.recommendation}\nWhy: {recommendation.reason}\nChoose Accept, Reject, or Skip.",
                "focus_decision",
                {"task_id": task.id,
                    "recommendation": recommendation.model_dump(mode="json")},
            )

        answer_text = (answer.get("answer") or "").strip()
        if not answer_text:
            ctx.append("focus_outcome", {
                       "task_id": task.id, "status": "stopped", "reason": "No answer received."}, produced_by="system")
            _append_state(ctx, "complete",
                          "The user did not provide a decision.")
            return RunState.FAILED

        kind = (answer_context or {}).get("kind")
        lowered = answer_text.lower()

        if kind == "focus_decision":
            if lowered in {"accept", "accepted", "start", "yes", "y"}:
                ctx.append("focus_decision", {
                           "task_id": task.id, "decision": "accepted", "answer": answer_text}, produced_by="user")
                ctx.append("focus_outcome", {"task_id": task.id, "status": "started", "duration_minutes": ctx.latest(
                    "focus_recommendation")["suggested_duration_minutes"]}, produced_by="user")
                _append_state(ctx, "working",
                              "The user accepted the recommended focus block.")
                return RunState.COMPLETE
            if lowered in {"skip", "skipped"}:
                ctx.append("focus_decision", {
                           "task_id": task.id, "decision": "skipped", "answer": answer_text}, produced_by="user")
                ctx.append("focus_outcome", {
                           "task_id": task.id, "status": "skipped"}, produced_by="user")
                _append_state(ctx, "complete",
                              "The user skipped this recommendation.")
                return RunState.COMPLETE
            ctx.append("focus_decision", {
                       "task_id": task.id, "decision": "rejected", "answer": answer_text}, produced_by="user")
            _append_state(ctx, "asking_reason",
                          "The user rejected the recommendation.")
            return _ask(
                ctx,
                "Why does this recommendation not work right now? Reply with a reason such as too difficult, too long, too tired, not urgent, or do not know where to start.",
                "rejection_reason",
                {"task_id": task.id},
            )

        if kind == "rejection_reason":
            ctx.append("rejection_event", {"task_id": task.id, "reason": answer_text, "context": {
                       "estimated_minutes": task.estimated_minutes}}, produced_by="user")
            _append_state(
                ctx, "learning", "The rejection reason was stored for future recommendations.")
            history = _history_payload(ctx.store)
            if any(word in lowered for word in ("big", "difficult", "long", "overwhelm", "start", "where to begin")):
                action = breakdown(ctx, task, answer_text, history)
                ctx.append("breakdown_action", action.model_dump(
                    mode="json"), produced_by="agent:nagare_focus")
                _append_state(ctx, "waiting_for_human",
                              "A smaller action is awaiting approval.")
                return _ask(
                    ctx,
                    f"Let's make it smaller. Next step: {action.step} ({action.estimated_minutes} minutes). Completion: {action.completion_condition}\nChoose Start this step or Not useful.",
                    "breakdown_decision",
                    {"task_id": task.id,
                        "breakdown": action.model_dump(mode="json")},
                )
            if len([item for item in history if item.get("kind") == "rejection_event" and item.get("task_id") == task.id]) >= MAX_REPLANS:
                ctx.append("manual_control", {
                           "task_id": task.id, "reason": "Too many rejected recommendations."}, produced_by="nagare_focus")
                _append_state(
                    ctx, "complete", "Control returned to the user after repeated rejection.")
                return RunState.COMPLETE
            recommendation = recommend(ctx, task, history)
            ctx.append("focus_recommendation", {**recommendation.model_dump(
                mode="json"), "task_id": task.id}, produced_by="agent:nagare_focus")
            _append_state(ctx, "waiting_for_human",
                          "A revised recommendation is awaiting a user decision.")
            return _ask(ctx, f"Revised recommendation: {recommendation.recommendation}\nWhy: {recommendation.reason}\nChoose Accept, Reject, or Skip.", "focus_decision", {"task_id": task.id, "recommendation": recommendation.model_dump(mode="json")})

        if kind == "breakdown_decision":
            accepted = lowered in {"start", "accept", "accepted", "yes", "y"}
            ctx.append("breakdown_decision", {
                       "task_id": task.id, "decision": "accepted" if accepted else "rejected", "answer": answer_text}, produced_by="user")
            if accepted:
                ctx.append("focus_outcome", {"task_id": task.id, "status": "started_breakdown", "breakdown": ctx.latest(
                    "breakdown_action")}, produced_by="user")
                _append_state(ctx, "working",
                              "The user accepted the smaller action.")
                return RunState.COMPLETE
            _append_state(ctx, "asking_reason",
                          "The user rejected the breakdown action.")
            return _ask(ctx, "Why is this smaller step not useful?", "breakdown_rejection_reason", {"task_id": task.id})

        if kind == "breakdown_rejection_reason":
            ctx.append("rejection_event", {
                       "task_id": task.id, "reason": answer_text, "source": "breakdown"}, produced_by="user")
            ctx.append("manual_control", {
                       "task_id": task.id, "reason": "The breakdown action was rejected."}, produced_by="nagare_focus")
            _append_state(
                ctx, "complete", "Control returned to the user after the breakdown was rejected.")
            return RunState.COMPLETE

        return RunState.FAILED

    return SimpleNamespace(name="nagare_focus", handlers={RunState.DRAFTING: handle_drafting})

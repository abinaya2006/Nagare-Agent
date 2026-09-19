from datetime import date

from demo.nagare.focus import (
    BreakdownAction,
    FocusRecommendation,
    build_focus_flow,
)
from slice import callback, runner
from slice.config import Settings
from slice.records import RunState
from slice.store import Store


def settings() -> Settings:
    return Settings(
        api_key="",
        model="model",
        fallback_model="fallback",
        escalation_model="escalation",
        max_tokens=100,
        max_tokens_per_run=2000,
        max_attempts_per_step=3,
        expert_timeout_minutes=45,
        langfuse_public="",
        langfuse_secret="",
        langfuse_host="",
    )


def start(store, task_id="ml"):
    run_id = store.create_run("nagare_focus")
    store.append(
        run_id,
        "input",
        {
            "selected_task_id": task_id,
            "tasks": [
                {
                    "id": task_id,
                    "title": "Finish ML assignment",
                    "description": "Complete questions 1-5",
                    "deadline": "2026-09-22",
                    "estimated_minutes": 90,
                    "priority": "high",
                    "status": "pending",
                }
            ],
            "context": {"time_of_day": "evening"},
        },
        produced_by="test",
    )
    return run_id


def answer(store, run_id, text):
    question = callback.pending(store, run_id)[0]
    callback.answer(store, question.id, text, who="test_user")
    return runner.advance(store, run_id, build_focus_flow(), settings())


def test_focus_recommends_exactly_selected_task_and_pauses(tmp_path):
    store = Store(tmp_path / "focus.db")
    run_id = start(store)

    state = runner.advance(store, run_id, build_focus_flow(), settings())

    assert state is RunState.AWAITING_EXPERT
    recommendation = store.latest(run_id, "focus_recommendation")
    assert recommendation["task_id"] == "ml"
    assert len(store.history(run_id, "focus_recommendation")) == 1
    assert "Accept" in callback.pending(store, run_id)[0].question


def test_accept_records_started_outcome(tmp_path):
    store = Store(tmp_path / "accept.db")
    run_id = start(store)
    runner.advance(store, run_id, build_focus_flow(), settings())

    assert answer(store, run_id, "accept") is RunState.COMPLETE
    assert store.latest(run_id, "focus_decision")["decision"] == "accepted"
    assert store.latest(run_id, "focus_outcome")["status"] == "started"


def test_rejection_asks_reason_then_breaks_down_task(tmp_path):
    store = Store(tmp_path / "breakdown.db")
    run_id = start(store)
    runner.advance(store, run_id, build_focus_flow(), settings())

    assert answer(store, run_id, "reject") is RunState.AWAITING_EXPERT
    assert "Why" in callback.pending(store, run_id)[0].question
    assert answer(
        store, run_id, "It is too big and I do not know where to start") is RunState.AWAITING_EXPERT
    action = store.latest(run_id, "breakdown_action")
    assert action["parent_task_id"] == "ml"
    assert 5 <= action["estimated_minutes"] <= 30
    assert answer(store, run_id, "start") is RunState.COMPLETE
    assert store.latest(run_id, "breakdown_decision")["decision"] == "accepted"
    assert store.latest(run_id, "focus_outcome")[
        "status"] == "started_breakdown"


def test_rejection_memory_changes_future_recommendation(tmp_path):
    store = Store(tmp_path / "memory.db")
    first_run = start(store)
    runner.advance(store, first_run, build_focus_flow(), settings())
    answer(store, first_run, "reject")
    answer(store, first_run, "too long")
    assert answer(store, first_run, "start") is RunState.COMPLETE

    second_run = start(store)
    assert runner.advance(store, second_run, build_focus_flow(),
                          settings()) is RunState.AWAITING_EXPERT
    recommendation = store.latest(second_run, "focus_recommendation")
    assert recommendation["suggested_duration_minutes"] == 20


def test_breakdown_rejection_is_recorded_and_returns_control(tmp_path):
    store = Store(tmp_path / "breakdown-reject.db")
    run_id = start(store)
    runner.advance(store, run_id, build_focus_flow(), settings())
    answer(store, run_id, "reject")
    answer(store, run_id, "too big")

    assert answer(store, run_id, "not useful") is RunState.AWAITING_EXPERT
    assert answer(
        store, run_id, "I need a different task") is RunState.COMPLETE
    assert store.latest(run_id, "manual_control") is not None
    assert store.latest(run_id, "rejection_event")["source"] == "breakdown"


def test_replanning_stops_after_three_non_overwhelm_rejections(tmp_path):
    store = Store(tmp_path / "bounded.db")
    run_id = start(store)
    runner.advance(store, run_id, build_focus_flow(), settings())

    for _ in range(3):
        assert answer(store, run_id, "reject") is RunState.AWAITING_EXPERT
        state = answer(store, run_id, "not urgent")
    assert state is RunState.COMPLETE
    assert store.latest(run_id, "manual_control") is not None


def test_ai_recommendation_is_typed_and_keeps_selected_task(tmp_path):
    store = Store(tmp_path / "ai.db")
    run_id = start(store)
    calls = []

    def fake_ai(**kwargs):
        calls.append(kwargs)
        return FocusRecommendation(
            task_id="ml",
            recommendation="Work on question 1 for 25 minutes.",
            reason="A short first step fits the deadline.",
            suggested_duration_minutes=25,
        )

    assert runner.advance(store, run_id, build_focus_flow(
        fake_ai), settings()) is RunState.AWAITING_EXPERT
    assert calls[0]["schema"] is FocusRecommendation
    assert store.latest(run_id, "focus_recommendation")[
        "suggested_duration_minutes"] == 25

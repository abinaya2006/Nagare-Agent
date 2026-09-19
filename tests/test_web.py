from fastapi.testclient import TestClient

from slice.store import Store
from web import expert


def test_schedule_form_generates_and_reschedules(tmp_path):
    expert.DB = str(tmp_path / "web.db")
    client = TestClient(expert.app)

    response = client.post(
        "/schedule",
        data={
            "day": "2026-09-19",
            "available_start": "09:00",
            "available_end": "17:00",
            "morning_energy": "5",
            "afternoon_energy": "3",
            "evening_energy": "2",
            "tasks": "Write report | 60 | 5 | 17:00\nRead notes | 30 | 2",
        },
    )

    assert response.status_code == 200
    assert "Your schedule" in response.text
    assert "Write report" in response.text

    store = Store(expert.DB)
    run = store.list_runs(limit=1)[0]
    rescheduled = client.post(
        f"/runs/{run['id']}/reschedule",
        data={"task_id": "task-001", "reason": "Meeting ran late"},
    )

    assert rescheduled.status_code == 200
    assert "Schedule updated" in rescheduled.text
    assert len(store.list_runs(limit=2)) == 2


def test_schedule_does_not_spill_tasks_into_tomorrow(tmp_path):
    expert.DB = str(tmp_path / "single-day.db")
    client = TestClient(expert.app)

    response = client.post(
        "/schedule",
        data={
            "day": "2026-09-19",
            "available_start": "09:00",
            "available_end": "11:00",
            "morning_energy": "5",
            "afternoon_energy": "3",
            "evening_energy": "2",
            "tasks": (
                "Study Computer Networks | 90 | 3\n"
                "Finish project report | 120 | 5\n"
                "Reply to emails | 30 | 2"
            ),
        },
    )

    assert response.status_code == 200
    store = Store(expert.DB)
    run = store.list_runs(limit=1)[0]
    schedule = store.latest(run["id"], "proposed_schedule")
    assert all(
        block["start"].startswith("2026-09-19T")
        and block["end"].startswith("2026-09-19T")
        for block in schedule["blocks"]
    )


def test_focus_web_flow_accepts_selected_task_and_records_outcome(tmp_path):
    expert.DB = str(tmp_path / "focus-web.db")
    client = TestClient(expert.app)

    schedule_response = client.post(
        "/schedule",
        data={
            "day": "2026-09-19",
            "available_start": "09:00",
            "available_end": "10:00",
            "morning_energy": "5",
            "afternoon_energy": "3",
            "evening_energy": "2",
            "protected_blocks": "Lunch | 09:30 | 09:45",
            "tasks": (
                "Study networks | 30 | 3\n"
                "Finish ML assignment | 90 | 5 | 09:50"
            ),
        },
    )

    assert schedule_response.status_code == 200
    store = Store(expert.DB)
    schedule_run = store.list_runs(limit=1)[0]
    profile = store.latest(schedule_run["id"], "input")["profile"]
    assert profile["protected_blocks"][0]["start"] == "2026-09-19T09:30:00"

    response = client.get("/focus")
    assert response.status_code == 200
    assert "Finish ML assignment" in response.text
    assert "Study networks" not in response.text
    assert "read-only" in response.text

    focus_response = client.post(
        "/focus",
        data={
            "selected_task_id": "task-002",
            "schedule_run_id": schedule_run["id"],
        },
    )
    assert focus_response.status_code == 200
    assert "Accept" in focus_response.text
    assert "Reject" in focus_response.text
    assert "Skip" in focus_response.text
    run = store.list_runs(limit=1)[0]
    assert run["domain"] == "nagare_focus"
    question = store.open_questions(run["id"])[0]

    answered = client.post(
        f"/q/{question.id}",
        data={"answer": "accept", "who": "user"},
    )

    assert answered.status_code == 200
    assert store.get_state(run["id"]).value == "complete"
    assert store.latest(run["id"], "focus_outcome")["status"] == "started"

    completed = client.post(
        f"/focus/runs/{run['id']}/outcome",
        data={"status": "completed"},
    )
    assert completed.status_code == 200
    assert store.latest(run["id"], "focus_outcome")["status"] == "completed"
    queue_after_completion = client.get("/focus")
    assert queue_after_completion.status_code == 200
    assert "Your pending queue is clear" in queue_after_completion.text
    assert "Finish ML assignment" not in queue_after_completion.text


def test_schedule_conflict_resumes_after_deferring_task(tmp_path):
    expert.DB = str(tmp_path / "conflict.db")
    client = TestClient(expert.app)

    response = client.post(
        "/schedule",
        data={
            "day": "2026-09-19",
            "available_start": "09:00",
            "available_end": "10:00",
            "morning_energy": "5",
            "afternoon_energy": "3",
            "evening_energy": "2",
            "tasks": "Study networks | 90 | 4 | 18:00\nReply to emails | 30 | 2",
        },
    )

    assert "awaiting_expert" in response.text
    assert "Move task to tomorrow" in response.text
    assert "Study networks" in response.text
    assert "usable minutes" in response.text

    store = Store(expert.DB)
    run = store.list_runs(limit=1)[0]
    question = store.open_questions(run["id"])[0]
    question_page = client.get(f"/q/{question.id}")
    assert question_page.status_code == 200
    assert "A decision is needed" in question_page.text
    answer = client.post(
        f"/q/{question.id}",
        data={"answer": "move the task to tomorrow", "who": "user"},
    )

    assert answer.status_code == 200
    assert "Got it" in answer.text
    assert store.get_state(run["id"]).value == "complete"
    assert store.latest(run["id"], "decision")["status"] == "scheduled"


def test_schedule_conflict_shortening_shows_updated_schedule(tmp_path):
    expert.DB = str(tmp_path / "shorten.db")
    client = TestClient(expert.app)

    response = client.post(
        "/schedule",
        data={
            "day": "2026-09-19",
            "available_start": "11:00",
            "available_end": "14:00",
            "morning_energy": "5",
            "afternoon_energy": "3",
            "evening_energy": "2",
            "tasks": "Finish report | 240 | 5 | 14:00",
        },
    )

    store = Store(expert.DB)
    run = store.list_runs(limit=1)[0]
    question = store.open_questions(run["id"])[0]
    updated = client.post(
        f"/q/{question.id}",
        data={"answer": "shorten the task to 120 minutes", "who": "user"},
    )

    assert updated.status_code == 200
    assert "Got it" in updated.text
    assert "Finish report" in updated.text
    assert store.get_state(run["id"]).value == "complete"
    assert store.latest(run["id"], "decision")["status"] == "scheduled"

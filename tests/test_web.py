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
    assert "Answer this decision" in response.text

    store = Store(expert.DB)
    run = store.list_runs(limit=1)[0]
    question = store.open_questions(run["id"])[0]
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

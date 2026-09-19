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

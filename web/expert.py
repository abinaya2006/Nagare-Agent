"""
The page a user answers on when NANI can't resolve a schedule conflict alone.

This is the human-in-the-loop half of Nagare's back-edge. The agent suspends a
run (AWAITING_EXPERT) and parks a question when it hits a conflict it cannot
safely resolve on its own - e.g. a hard-deadline task that has no available
window without violating sleep or a protected block. The user opens this page,
answers, and the run resumes with their answer recorded as a distinct class of
evidence: not something NANI inferred, something the user actually said.

Unlike the smoke demo's original framing, the person answering here isn't a
third-party domain expert - it's the same user whose schedule it is. Same
callback mechanism, different actor, so the copy below speaks to "you" and to
schedule conflicts specifically rather than startup theses.

Server-rendered, no JavaScript, no build step. The user may be on a phone
between tasks, and this has to work there.

Run it, then expose it:

    uvicorn web.expert:app --host 0.0.0.0 --port 8000
    cloudflared tunnel --url http://localhost:8000

The tunnel needs no account and prints a public URL. For the hackathon demo
that URL can be sent to whoever's schedule is being tested; in a real
deployment this page would just be a view inside Nagare's own app instead of a
separate tunneled link.
"""
from __future__ import annotations

import html
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from demo.nagare.flow import build_flow
from demo.nagare.focus import FocusTask, build_focus_flow
from demo.nagare.schema import CircadianProfile, Task, TimeWindow, UserScheduleProfile
from slice import callback, runner
from slice.config import settings as load_settings
from slice.records import RunState
from slice.store import Store

DB = os.environ.get("SLICE_DB", "run.db")
app = FastAPI(title="Nagare — schedule check-in")
SESSION_SECRET = os.environ.get(
    "NAGARE_SESSION_SECRET", "local-development-secret")
REQUEST_LIMIT = 60
MODEL_CALL_LIMIT = 20
_REQUESTS = {}
_MODEL_CALLS = {}


def _store() -> Store:
    return Store(DB)


def _auth_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)")
    db.commit()
    return db


def _password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return base64.urlsafe_b64encode(salt + digest).decode()


def _password_matches(password: str, encoded: str) -> bool:
    raw = base64.urlsafe_b64decode(encoded.encode())
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), raw[:16], 200_000)
    return hmac.compare_digest(raw[16:], digest)


def _session_value(user_id: str) -> str:
    payload = f"{user_id}:{int(clock.time()) + 86400}"
    signature = hmac.new(SESSION_SECRET.encode(),
                         payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def _session_user(request: Request) -> str | None:
    token = request.cookies.get("nagare_session")
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, expires, signature = raw.rsplit(":", 2)
        payload = f"{user_id}:{expires}"
        expected = hmac.new(SESSION_SECRET.encode(),
                            payload.encode(), hashlib.sha256).hexdigest()
        if int(expires) < int(clock.time()) or not hmac.compare_digest(signature, expected):
            return None
        return user_id
    except (ValueError, TypeError):
        return None


def _csrf_valid(request: Request) -> bool:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host", "")
    if origin and host not in origin:
        return False
    if referer and host not in referer:
        return False
    supplied = request.headers.get("x-csrf-token")
    return bool(supplied and hmac.compare_digest(supplied, request.cookies.get("nagare_csrf", "")))


def _allow_request(user_id: str) -> bool:
    now = clock.time()
    recent = [value for value in _REQUESTS.get(
        user_id, []) if now - value < 60]
    if len(recent) >= REQUEST_LIMIT:
        return False
    recent.append(now)
    _REQUESTS[user_id] = recent
    return True


@app.middleware("http")
async def require_session(request: Request, call_next):
    if request.url.path in {"/login", "/register"}:
        return await call_next(request)
    user_id = _session_user(request)
    if user_id is None:
        return HTMLResponse("Authentication required", status_code=401)
    if not _allow_request(user_id):
        return HTMLResponse("Request limit exceeded", status_code=429)
    if request.method == "POST" and not _csrf_valid(request):
        return HTMLResponse("CSRF validation failed", status_code=403)
    request.state.user_id = user_id
    return await call_next(request)


def _owned_run(store: Store, run_id: str, user_id: str) -> bool:
    try:
        return store.meta(run_id).get("owner_id") == user_id
    except KeyError:
        return False


def _require_owned(store: Store, run_id: str, user_id: str) -> None:
    if not _owned_run(store, run_id, user_id):
        raise HTTPException(status_code=404, detail="Not found")


def _allow_model_call(user_id: str) -> bool:
    now = clock.time()
    recent = [value for value in _MODEL_CALLS.get(
        user_id, []) if now - value < 3600]
    if len(recent) >= MODEL_CALL_LIMIT:
        return False
    recent.append(now)
    _MODEL_CALLS[user_id] = recent
    return True


def _auth_page(message: str = "") -> HTMLResponse:
    return _page(
        "Log in",
        "<h1>Log in to Nagare</h1>"
        f"<p class='error'>{html.escape(message)}</p>" if message else
        "<h1>Log in to Nagare</h1>"
        "<form method='post' action='/login'>"
        "<label>Username<input name='username' required></label>"
        "<label>Password<input type='password' name='password' required></label>"
        "<button type='submit'>Log in</button></form>"
        "<p><a href='/register'>Create a local account</a></p>",
    )


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return _auth_page()


@app.post("/login", response_class=HTMLResponse)
def login(username: str = Form(...), password: str = Form(...)):
    db = _auth_db()
    row = db.execute("SELECT id, password_hash FROM users WHERE username=?",
                     (username.strip(),)).fetchone()
    db.close()
    if row is None or not _password_matches(password, row[1]):
        return _auth_page("Invalid username or password.")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("nagare_session", _session_value(
        row[0]), httponly=True, samesite="lax")
    response.set_cookie("nagare_csrf", base64.urlsafe_b64encode(
        os.urandom(24)).decode(), httponly=False, samesite="lax")
    return response


@app.get("/register", response_class=HTMLResponse)
def register_form():
    return _page(
        "Create account",
        "<h1>Create a local account</h1>"
        "<form method='post' action='/register'>"
        "<label>Username<input name='username' required></label>"
        "<label>Password<input type='password' name='password' minlength='10' required></label>"
        "<button type='submit'>Create account</button></form>",
    )


@app.post("/register", response_class=HTMLResponse)
def register(username: str = Form(...), password: str = Form(...)):
    if len(password) < 10 or not username.strip():
        return _page("Account error", "<h1>Use a username and a 10-character password.</h1>")
    user_id = hashlib.sha256(
        f"{username}:{clock.time()}".encode()).hexdigest()[:24]
    db = _auth_db()
    try:
        db.execute("INSERT INTO users(id, username, password_hash) VALUES (?,?,?)",
                   (user_id, username.strip(), _password_hash(password)))
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        return _page("Account error", "<h1>That username is already in use.</h1>")
    db.close()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("nagare_session", _session_value(
        user_id), httponly=True, samesite="lax")
    response.set_cookie("nagare_csrf", base64.urlsafe_b64encode(
        os.urandom(24)).decode(), httponly=False, samesite="lax")
    return response


def _pending_tasks(payload: dict, schedule: dict) -> list[dict]:
    scheduled_minutes = {}
    for block in schedule.get("blocks", []):
        if block.get("block_type") != "task" or not block.get("task_id"):
            continue
        start = datetime.fromisoformat(block["start"])
        end = datetime.fromisoformat(block["end"])
        minutes = int((end - start).total_seconds() // 60)
        scheduled_minutes[block["task_id"]] = scheduled_minutes.get(
            block["task_id"], 0) + minutes

    pending = []
    for task in payload.get("tasks", []):
        remaining = schedule.get("pending_minutes", {}).get(task["id"])
        if remaining is None:
            remaining = task["estimated_duration"] - \
                scheduled_minutes.get(task["id"], 0)
        if remaining <= 0:
            continue
        pending.append({
            "id": task["id"],
            "title": task["title"],
            "description": task.get("description"),
            "deadline": task.get("deadline"),
            "estimated_minutes": remaining,
            "priority": task.get("priority", "medium"),
            "status": "pending",
        })
    return pending


def _datetime(day: str, value: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(value))


def _profile(day: str, available_start: str, available_end: str,
             morning_energy: int, afternoon_energy: int,
             evening_energy: int, task_break_minutes: int = 15,
             protected_blocks: list[TimeWindow] | None = None) -> UserScheduleProfile:

    start_today = _datetime(day, available_start)
    end_today = _datetime(day, available_end)
    noon_today = _datetime(day, "12:00")
    peak_end_today = min(
        end_today, noon_today) if start_today < noon_today else end_today

    return UserScheduleProfile(
        available_windows=[
            TimeWindow(start=start_today, end=end_today),
        ],
        circadian_profile=CircadianProfile(
            morning_energy=morning_energy,
            afternoon_energy=afternoon_energy,
            evening_energy=evening_energy,
            peak_periods=[TimeWindow(start=start_today, end=peak_end_today)],
        ),
        preferred_work_periods=[
            TimeWindow(start=start_today, end=peak_end_today),
        ],
        protected_blocks=protected_blocks or [],
        preferred_session_length=60,
        preferred_break_length=15,
        task_break_minutes=task_break_minutes,
        sleep_window=TimeWindow(
            start=_datetime(day, "23:00"),
            end=_datetime(day, "23:00") + timedelta(hours=8),
        ),
        commute_windows=[],
    )


def _protected_blocks(day: str, raw_blocks: str) -> list[TimeWindow]:
    parsed = []
    for line in raw_blocks.splitlines():
        parts = [part.strip() for part in line.split("|", 2)]
        if not parts or not parts[0]:
            continue
        if len(parts) != 3:
            raise ValueError("Protected moments must use: name | start | end")
        start = _datetime(day, parts[1])
        end = _datetime(day, parts[2])
        parsed.append(TimeWindow(start=start, end=end))
    return parsed


def _tasks(day: str, raw_tasks: str, available_start: str) -> list[Task]:
    parsed = []
    for index, line in enumerate(raw_tasks.splitlines(), 1):
        parts = [part.strip() for part in line.split("|")]
        if not parts or not parts[0]:
            continue
        if len(parts) < 2:
            raise ValueError(
                "Each task must use: title | minutes | priority | deadline")
        duration = int(parts[1])
        priority = int(parts[2]) if len(parts) > 2 and parts[2] else 3
        deadline_text = parts[3].lower() if len(parts) > 3 else ""
        no_deadline = {"", "none", "no deadline", "n/a", "na", "-"}
        deadline = (
            None
            if deadline_text in no_deadline
            else _datetime(day, deadline_text)
        )

        parsed.append(Task(
            id=f"task-{index:03d}",
            title=parts[0],
            estimated_duration=duration,
            priority=priority,
            consequence_of_delay=priority,
            energy_required=min(priority, 5),
            earliest_start=_datetime(day, available_start),
            deadline=deadline,
            deadline_type="hard" if deadline else "none",
        ))
    if not parsed:
        raise ValueError("Add at least one task.")
    return parsed


def _task_rows(day: str, titles: list[str], minutes: list[int],
               priorities: list[int], deadlines: list[str],
               available_start: str) -> list[Task]:
    rows = []
    for index, title in enumerate(titles):
        if not title.strip():
            continue
        rows.append(_tasks(
            day,
            " | ".join([
                title.strip(),
                str(minutes[index]),
                str(priorities[index]),
                deadlines[index] if index < len(deadlines) else "none",
            ]),
            available_start,
        )[0])
    if not rows:
        raise ValueError("Choose or add at least one task.")
    return [task.model_copy(update={"id": f"task-{index:03d}"})
            for index, task in enumerate(rows, 1)]


def _run_schedule(payload: dict, owner_id: str) -> tuple[str, RunState]:
    store = _store()
    run_id = store.create_run("nagare", meta={"owner_id": owner_id})
    store.append(run_id, "input", payload, produced_by="web_user")
    settings = load_settings()
    call = None
    if (os.environ.get("NAGARE_AGENT_MODE", "model").lower() == "model"
            and settings.api_key and _allow_model_call(owner_id)):
        from slice.llm import complete
        call = complete
    return run_id, runner.advance(store, run_id, build_flow(call), settings)


def _focus_model_call(settings, owner_id: str | None = None):
    if (os.environ.get("NAGARE_AGENT_MODE", "model").lower() == "model"
            and settings.api_key
            and (owner_id is None or _allow_model_call(owner_id))):
        from slice.llm import complete
        return complete
    return None


def _run_focus(payload: dict, owner_id: str) -> tuple[str, RunState]:
    store = _store()
    run_id = store.create_run("nagare_focus", meta={"owner_id": owner_id})
    store.append(run_id, "input", payload, produced_by="web_user")
    settings = load_settings()
    return run_id, runner.advance(
        store, run_id, build_focus_flow(_focus_model_call(settings, owner_id)), settings)


def _focus_html(run_id: str) -> str:
    store = _store()
    state = store.get_state(run_id)
    payload = store.latest(run_id, "input") or {}
    selected_id = payload.get("selected_task_id")
    task = next(
        (item for item in payload.get("tasks", [])
         if item.get("id") == selected_id),
        {"id": selected_id, "title": "Selected task"},
    )
    recommendation = store.latest(run_id, "focus_recommendation")
    breakdown = store.latest(run_id, "breakdown_action")
    pending = callback.pending(store, run_id)
    decision = ""
    if pending:
        question = pending[0]
        if question.context.get("kind") == "focus_decision":
            decision_controls = (
                f"<form method='post' action='/q/{html.escape(question.id)}' class='decision-actions'>"
                "<input type='hidden' name='who' value='user'>"
                "<button type='submit' name='answer' value='accept'>Accept</button>"
                "<button type='submit' name='answer' value='reject' class='button-secondary'>Reject</button>"
                "<button type='submit' name='answer' value='skip' class='button-quiet'>Skip</button></form>"
            )
        elif question.context.get("kind") == "breakdown_decision":
            decision_controls = (
                f"<form method='post' action='/q/{html.escape(question.id)}' class='decision-actions'>"
                "<input type='hidden' name='who' value='user'>"
                "<button type='submit' name='answer' value='start'>Start this step</button>"
                "<button type='submit' name='answer' value='not useful' class='button-secondary'>Not useful</button></form>"
            )
        else:
            decision_controls = f"<a class='nav-link' href='/q/{html.escape(question.id)}'>Write an answer</a>"
        decision = (
            "<div class='card decision-card'><p class='kicker'>Human checkpoint</p><h2>Your decision is needed</h2>"
            f"<p class='q'>{html.escape(question.question)}</p>"
            f"{decision_controls}</div>"
        )
    recommendation_html = ""
    if recommendation:
        recommendation_html = (
            "<div class='card recommendation-card'><p class='kicker'>Nagare recommends</p><h2>Current recommendation</h2>"
            f"<p class='q'>{html.escape(recommendation['recommendation'])}</p>"
            f"<p class='sub'>{html.escape(recommendation['reason'])}</p></div>"
        )
    breakdown_html = ""
    if breakdown:
        breakdown_html = (
            "<div class='card breakdown-card'><p class='kicker'>Make it smaller</p><h2>Smaller next action</h2>"
            f"<p class='q'>{html.escape(breakdown['step'])}</p>"
            f"<p class='sub'>{breakdown['estimated_minutes']} minutes. "
            f"{html.escape(breakdown['completion_condition'])}</p></div>"
        )
    outcome = store.latest(run_id, "focus_outcome")
    outcome_form = ""
    if outcome and outcome.get("status") in {
        "started", "started_breakdown", "partially_completed"
    }:
        outcome_form = (
            "<div class='card'><h2>When you finish</h2>"
            f"<form method='post' action='/focus/runs/{html.escape(run_id)}/outcome'>"
            "<label>Minutes actually worked<input type='number' name='worked_minutes' min='0' required></label>"
            "<select name='status'><option value='completed'>Completed</option>"
            "<option value='partially_completed'>Partially completed</option>"
            "<option value='not_completed'>Not completed</option></select>"
            "<button type='submit'>Record outcome</button></form></div>"
        )
    return (
        f"<h1>Focus with Nagare</h1><p class='sub'>Run {html.escape(run_id)} · "
        f"Status: <span class='status'>{html.escape(state.value)}</span></p>"
        f"<div class='card'><h2>Selected task</h2><p class='q'>{html.escape(task.get('title', ''))}</p>"
        f"<p class='sub'>{html.escape(task.get('description') or '')}</p></div>"
        f"{recommendation_html}{breakdown_html}{decision}{outcome_form}"
        "<p><a href='/focus'>View pending queue</a> · <a href='/schedule'>Start a new schedule</a> · <a href='/'>Pending decisions</a></p>"
    )


def _latest_schedule_queue(user_id: str) -> tuple[str | None, list[dict]]:
    store = _store()
    for run in store.list_runs(limit=100):
        if run["domain"] != "nagare":
            continue
        if not _owned_run(store, run["id"], user_id):
            continue
        payload = store.latest(run["id"], "input") or {}
        schedule = store.latest(run["id"], "proposed_schedule") or {}
        completed_ids = set()
        consumed_minutes = {}
        for focus_run in store.list_runs(limit=100):
            if focus_run["domain"] != "nagare_focus":
                continue
            if not _owned_run(store, focus_run["id"], user_id):
                continue
            focus_input = store.latest(focus_run["id"], "input") or {}
            if focus_input.get("context", {}).get("source_schedule_run_id") != run["id"]:
                continue
            task_id = focus_input.get("selected_task_id")
            outcome = store.latest(focus_run["id"], "focus_outcome") or {}
            status = outcome.get("status")
            if status == "completed":
                completed_ids.add(task_id)
            elif status == "partially_completed":
                consumed_minutes[task_id] = consumed_minutes.get(task_id, 0) + outcome.get(
                    "worked_minutes", 0
                )

        pending = []
        for task in _pending_tasks(payload, schedule):
            if task["id"] in completed_ids:
                continue
            remaining = task["estimated_minutes"] - \
                consumed_minutes.get(task["id"], 0)
            if remaining > 0:
                pending.append({**task, "estimated_minutes": remaining})
        return run["id"], pending
    return None, []


@app.get("/focus", response_class=HTMLResponse)
def focus_form(request: Request):
    schedule_run_id, pending_tasks = _latest_schedule_queue(
        request.state.user_id)
    if schedule_run_id is None:
        return _page(
            "Pending queue",
            "<h1>Start with your schedule</h1>"
            "<p class='sub'>Your pending queue is created from tasks that could not be placed in your latest schedule.</p>"
            "<a class='nav-link' href='/schedule'>Set up today\'s schedule</a>",
        )
    if not pending_tasks:
        return _page(
            "Pending queue",
            "<h1>Your pending queue is clear</h1>"
            "<p class='sub'>Every task from the latest schedule was placed.</p>"
            "<a class='nav-link' href='/schedule'>Start another schedule</a>",
        )
    options = "".join(
        f"<option value='{html.escape(task['id'])}'>{html.escape(task['title'])} · "
        f"{html.escape(str(task['estimated_minutes']))} min</option>"
        for task in pending_tasks
    )
    return _page(
        "Choose a focus task",
        "<div class='focus-hero'><p class='kicker'>Your next move</p>"
        "<h1>Your pending queue</h1>"
        "<p class='sub'>These tasks were not placed in the latest schedule. The queue is read-only; choose one and Nagare will suggest how to begin.</p></div>"
        "<form method='post' action='/focus' class='focus-form'>"
        f"<input type='hidden' name='schedule_run_id' value='{html.escape(schedule_run_id)}'>"
        "<div class='card'><p class='kicker'>Select one pending task</p>"
        f"<label>Task<select name='selected_task_id' required>{options}</select></label>"
        "<button type='submit'>Get my next step</button></div></form>"
        "<p><a href='/'>Pending decisions</a></p>",
    )


@app.post("/focus", response_class=HTMLResponse)
def create_focus(request: Request, selected_task_id: str = Form(...), schedule_run_id: str = Form(...)):
    try:
        store = _store()
        if store.get_domain(schedule_run_id) != "nagare":
            raise ValueError("That schedule run is not a Nagare schedule.")
        _require_owned(store, schedule_run_id, request.state.user_id)
        source = store.latest(schedule_run_id, "input") or {}
        schedule = store.latest(schedule_run_id, "proposed_schedule") or {}
        parsed = _pending_tasks(source, schedule)
        if selected_task_id not in {item["id"] for item in parsed}:
            raise ValueError("Select a task from the read-only pending queue.")
        run_id, _ = _run_focus(
            {"tasks": parsed, "selected_task_id": selected_task_id,
             "context": {"source_schedule_run_id": schedule_run_id}},
            request.state.user_id,
        )
        return _page("Focus recommendation", _focus_html(run_id))
    except (ValueError, TypeError):
        return _page(
            "Focus input error",
            "<h1>Could not start focus</h1>"
            "<p class='error'>Check the task format and selected task ID.</p>"
            "<p><a href='/focus'>Back to focus</a></p>",
        )


@app.get("/focus/runs/{run_id}", response_class=HTMLResponse)
def show_focus_run(request: Request, run_id: str):
    try:
        store = _store()
        if store.get_domain(run_id) != "nagare_focus":
            raise KeyError(run_id)
        _require_owned(store, run_id, request.state.user_id)
    except KeyError:
        return _page("Not found", "<h1>Not found</h1><p class='sub'>No such focus run.</p>")
    return _page("Focus recommendation", _focus_html(run_id))


@app.post("/focus/runs/{run_id}/outcome", response_class=HTMLResponse)
def record_focus_outcome(
    request: Request,
    run_id: str,
    status: str = Form(...),
    worked_minutes: int = Form(0),
):
    store = _store()
    if store.get_domain(run_id) != "nagare_focus":
        return _page("Not found", "<h1>Not found</h1>")
    _require_owned(store, run_id, request.state.user_id)
    if status not in {"completed", "partially_completed", "not_completed"}:
        return _page("Invalid outcome", "<h1>Invalid outcome</h1>")
    task_input = store.latest(run_id, "input") or {}
    task_id = task_input.get("selected_task_id")
    task = next(
        (item for item in task_input.get("tasks", []) if item.get("id") == task_id),
        None,
    )
    remaining = int((task or {}).get("estimated_minutes", 0))
    previous_work = sum(
        int(item.payload.get("worked_minutes", 0))
        for item in store.history(run_id, "focus_outcome")
        if item.payload.get("status") == "partially_completed"
    )
    remaining = max(0, remaining - previous_work)
    latest_outcome = store.latest(run_id, "focus_outcome")
    if latest_outcome and (
        latest_outcome.get("status") in {"completed", "not_completed"}
        or (
            latest_outcome.get("status") == "partially_completed"
            and status == "partially_completed"
        )
    ):
        return _page("Outcome already recorded", _focus_html(run_id))
    if worked_minutes < 0 or worked_minutes > remaining:
        return _page("Invalid outcome", "<h1>Minutes worked must be within the remaining task time.</h1>")
    if status == "partially_completed" and worked_minutes == 0:
        return _page("Invalid outcome", "<h1>Partial completion requires worked minutes.</h1>")
    if status == "completed" and worked_minutes != remaining:
        return _page("Invalid outcome", "<h1>Completed tasks must report all remaining minutes.</h1>")
    store.append(run_id, "focus_outcome", {
                 "task_id": task_id, "status": status,
                 "worked_minutes": worked_minutes},
                 produced_by="user")
    return _page("Outcome recorded", _focus_html(run_id))

# --- (The rest of the UI rendering code in web/expert.py remains identical) ---


def _schedule_html(run_id: str) -> str:
    store = _store()
    state = store.get_state(run_id)
    input_data = store.latest(run_id, "input") or {}
    schedule = store.latest(run_id, "proposed_schedule") or {}
    failure = store.latest(run_id, "failure")
    tasks = {task["id"]: task for task in input_data.get("tasks", [])}

    def render_row(block: dict, replaced: bool = False) -> str:
        title = html.escape(tasks.get(
            block.get("task_id"), {}).get("title", block.get("block_type", "block")))
        time_label = (
            f"{html.escape(block['start'][8:16].replace('T', ' '))} - "
            f"{html.escape(block['end'][11:16])}"
        )
        status = "Replaced" if replaced else (
            "Locked" if block.get("locked") else "Movable"
        )
        row_class = " class='replaced-row'" if replaced else ""
        return (
            f"<tr{row_class}><td>{time_label}</td>"
            f"<td>{'<s>' + title + '</s>' if replaced else title}</td>"
            f"<td>{status}</td></tr>"
        )

    rows = "".join(
        render_row(block)
        for block in schedule.get("blocks", [])
    )
    replaced_task_id = input_data.get("rescheduled_task_id")
    previous_schedule = input_data.get("previous_schedule", {})
    if replaced_task_id:
        rows = "".join(
            render_row(block, replaced=True)
            for block in previous_schedule.get("blocks", [])
            if block.get("task_id") == replaced_task_id
        ) + rows
    rows = rows or "<tr><td colspan='3'>No task could be placed in the available windows.</td></tr>"

    options = "".join(
        f"<option value='{html.escape(task_id)}'>{html.escape(task['title'])}</option>"
        for task_id, task in tasks.items()
    )

    pending = callback.pending(store, run_id)
    decision = ""
    if pending:
        question = pending[0]
        conflict_items = question.context.get("conflicts", [])
        diagnosis = question.context.get("diagnosis")
        conflict_html = (
            f"<div class='conflict-item'><strong>Nagare found a conflict</strong>"
            f"<span>{html.escape(diagnosis)}</span></div>"
            if diagnosis else ""
        )
        has_fragmentation = any(
            item.get("conflict_type") == "fragmentation"
            for item in conflict_items
        )
        if has_fragmentation:
            recommended_id = question.context.get("recommended_task_id")
            split_value = "split " + recommended_id if recommended_id else "fragment the task"
            decision_controls = (
                f"<form method='post' action='/q/{html.escape(question.id)}' class='decision-actions'>"
                "<input type='hidden' name='who' value='user'>"
                f"<button type='submit' name='answer' value='{html.escape(split_value)}'>Split recommended task</button>"
                "<button type='submit' name='answer' value='move the task to tomorrow' class='button-secondary'>Move task to tomorrow</button>"
                f"<a class='button-link' href='/q/{html.escape(question.id)}'>Other answer</a></form>"
            )
        elif conflict_items:
            decision_controls = (
                f"<form method='post' action='/q/{html.escape(question.id)}' class='decision-actions'>"
                "<input type='hidden' name='who' value='user'>"
                "<button type='submit' name='answer' value='move the task to tomorrow'>Move task to tomorrow</button>"
                "<button type='submit' name='answer' value='leave it missed' class='button-secondary'>Keep pending</button>"
                f"<a class='button-link' href='/q/{html.escape(question.id)}'>Other answer</a></form>"
            )
        else:
            decision_controls = f"<a class='button-link' href='/q/{html.escape(question.id)}'>Write an answer</a>"
        decision = (
            "<div class='card decision-card'><p class='kicker'>Human checkpoint</p><h2>Your decision is needed</h2>"
            f"<p class='sub'>{html.escape(question.question)}</p>"
            f"{conflict_html}{decision_controls}</div>"
        )
    failure_notice = ""
    if failure:
        failure_notice = (
            "<div class='error'><strong>Run stopped safely.</strong> "
            f"{html.escape(failure.get('detail', 'The planner could not finish.'))}</div>"
        )
    return (
        f"<h1>Your schedule</h1><p class='sub'>Run {html.escape(run_id)} · "
        f"Status: <span class='status'>{html.escape(state.value)}</span></p>"
        "<p class='sub'><strong>Got it.</strong> Here is the latest schedule after your decision.</p>"
        f"{failure_notice}"
        f"{decision}"
        "<div class='card'><table><thead><tr><th>Time</th><th>Task</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        "<div class='card'><h2>Add tasks</h2>"
        "<p class='hint'>New tasks will be scheduled around the blocks already shown.</p>"
        f"<form method='post' action='/runs/{html.escape(run_id)}/add-tasks'>"
        "<textarea name='tasks' required placeholder='Task title | minutes | priority | deadline\nReview notes | 30 | 2 | none'></textarea>"
        "<button type='submit'>Add and reschedule</button></form></div>"
        "<div class='card'><h2>Something changed?</h2>"
        f"<form method='post' action='/runs/{html.escape(run_id)}/reschedule'>"
        f"<label>Missed task<select name='task_id'>{options}</select></label>"
        "<label>What happened?<input name='reason' placeholder='Lab ran late'></label>"
        "<button type='submit'>Reschedule task</button></form></div>"
        "<p><a href='/schedule'>Create another schedule</a> · <a href='/'>Pending decisions</a></p>"
    )


PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 :root{{color-scheme:light;--ink:#173b3d;--muted:#667577;--paper:#f6f2e9;
--panel:#fffdf8;--line:#d9dfd6;--teal:#0c6664;--teal-dark:#084c4b;
--coral:#d86f52;--shadow:0 18px 50px rgba(26,62,58,.08)}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 "Trebuchet MS","Segoe UI",sans-serif}}
.shell{{max-width:58rem;margin:0 auto;padding:2rem 1.25rem 4rem}}
.masthead{{display:flex;align-items:center;justify-content:space-between;margin-bottom:3.2rem}}
.brand{{display:flex;align-items:center;gap:.7rem;color:var(--ink);font-weight:700;letter-spacing:.04em}}
.brand-mark{{display:grid;place-items:center;width:2.35rem;height:2.35rem;border-radius:12px;
background:var(--teal);color:#fff;font-family:Georgia,serif;font-size:1.35rem}}
.brand small{{display:block;color:var(--muted);font-size:.68rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase}}
.nav-link{{font-size:.82rem;font-weight:700;text-decoration:none;color:var(--teal)}}
.focus-hero{{padding:1.8rem 0 1.2rem;border-top:4px solid var(--coral)}}
.kicker{{margin:0 0 .35rem;color:var(--coral);font-size:.72rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase}}
.focus-form{{max-width:42rem}}
.recommendation-card{{border-left:5px solid var(--teal)}}
.breakdown-card{{border-left:5px solid var(--coral)}}
.decision-card{{background:#eef3ed;border-color:#b9c8be}}
.decision-actions{{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center}}
.decision-actions button{{margin-top:0}}
.button-secondary{{background:var(--coral)}}
.button-secondary:hover{{background:#b6533d}}
.button-quiet{{background:transparent;color:var(--teal);border:1px solid #9cb5ad;box-shadow:none}}
.button-quiet:hover{{background:#e1ece7}}
.button-link{{display:inline-flex;align-items:center;min-height:2.65rem;padding:.65rem 1rem;border:1px solid #9cb5ad;border-radius:8px;color:var(--teal);font-weight:700;text-decoration:none}}
.conflict-list{{display:grid;gap:.65rem;margin:1rem 0}}
.conflict-item{{display:grid;gap:.1rem;padding:.75rem .85rem;background:#fff;border-left:3px solid var(--coral);font-size:.9rem}}
.conflict-item span{{color:var(--muted)}}
h1{{font-family:Georgia,"Times New Roman",serif;font-size:clamp(2rem,5vw,3.4rem);line-height:1.05;
letter-spacing:-.02em;margin:0 0 .7rem;color:var(--ink)}}
h2{{font-size:1.05rem;margin:0 0 .75rem;color:var(--ink)}}
.sub{{color:var(--muted);font-size:1rem;max-width:42rem;margin:0 0 1.8rem}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:1.25rem 1.35rem;
margin:0 0 1rem;box-shadow:var(--shadow)}}
.q{{font-size:1.1rem;font-weight:600;margin:0 0 .8rem}}
.ctx{{background:#eef3ed;border-radius:9px;padding:.8rem 1rem;
font-size:.9rem;margin:0 0 1.2rem;white-space:pre-wrap;overflow-wrap:anywhere}}
.ctx b{{display:block;font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
color:var(--muted);margin-bottom:.35rem;font-weight:600}}
textarea{{width:100%;min-height:10rem;font:inherit;padding:.85rem;border:1px solid #b9c8be;
border-radius:9px;background:#fff;color:var(--ink);resize:vertical}}
button{{font:inherit;font-weight:700;padding:.72rem 1.15rem;margin-top:.8rem;
border:0;border-radius:8px;background:var(--teal);color:#fff;cursor:pointer;box-shadow:0 6px 14px rgba(12,102,100,.18)}}
button:hover{{background:var(--teal-dark)}}
a{{color:var(--teal)}} .empty{{color:var(--muted)}}
label{{display:block;font-weight:700;margin:.9rem 0;color:var(--ink);font-size:.9rem}}
input,select{{display:block;width:100%;font:inherit;padding:.72rem;margin-top:.35rem;
border:1px solid #b9c8be;border-radius:9px;background:#fff;color:var(--ink)}}
input:focus,select:focus,textarea:focus{{outline:3px solid rgba(216,111,82,.22);border-color:var(--coral)}}
table{{width:100%;border-collapse:collapse;font-size:.94rem}}
th,td{{text-align:left;padding:.8rem .55rem;border-bottom:1px solid var(--line)}}
th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
td:first-child{{font-weight:700;white-space:nowrap;color:var(--teal)}}
.hint{{font-size:.85rem;color:var(--muted);margin:.25rem 0 1rem}}
.error{{color:#8d3328;background:#fae3da;border-radius:9px;padding:.8rem 1rem}}
.note{{font-size:.85rem;color:var(--muted);margin-top:1.2rem}}
.status{{display:inline-block;padding:.22rem .55rem;border-radius:999px;background:#e5f2ea;color:#17634c;
font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em}}
.footer{{margin-top:2rem;color:var(--muted);font-size:.75rem}}
@media (max-width:560px){{.shell{{padding:1.1rem .9rem 3rem}}.masthead{{margin-bottom:2.1rem}}
.card{{padding:1rem}}table{{font-size:.82rem}}th,td{{padding:.65rem .25rem}}}}
</style>
<div class="shell"><header class="masthead"><a class="brand" href="/"><span class="brand-mark">N</span><span>NAGARE<small>daily flow planner</small></span></a><span><a class="nav-link" href="/focus">Focus</a> · <a class="nav-link" href="/schedule">Schedule</a></span></header>{body}<p class="footer">Nagare plans around your constraints, not against them.</p></div>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body))


@app.get("/schedule", response_class=HTMLResponse)
def schedule_form(timezone_name: str | None = None):
    try:
        selected_timezone = ZoneInfo(
            timezone_name or os.environ.get("NAGARE_TIMEZONE", "UTC"))
    except Exception:
        selected_timezone = timezone.utc
    return _page(
        "Build your schedule",
        "<h1>Build your schedule</h1>"
        "<p class='sub'>Tell Nagare when you are available and what needs to get done.</p>"
        "<form method='post' action='/schedule'>"
        f"<label>Date<input type='date' name='day' value='{datetime.now(selected_timezone).date().isoformat()}' required></label>"
        "<label>Available from<input type='time' name='available_start' value='09:00' required></label>"
        "<label>Available until<input type='time' name='available_end' value='21:00' required></label>"
        "<label>Break between tasks <output id='break-value'>15</output> min"
        "<input type='range' name='task_break_minutes' min='0' max='60' step='5' value='15' "
        "oninput=\"document.getElementById('break-value').value=this.value\"></label>"
        "<label>Morning energy (0-5)<input type='number' name='morning_energy' min='0' max='5' value='5' required></label>"
        "<label>Afternoon energy (0-5)<input type='number' name='afternoon_energy' min='0' max='5' value='3' required></label>"
        "<label>Evening energy (0-5)<input type='number' name='evening_energy' min='0' max='5' value='2' required></label>"
        "<fieldset><legend>Protected moments</legend>"
        "<div id='protected-rows'><div class='entry-row protected-row'>"
        "<select name='protected_name'><option value=''>No protected moment</option>"
        "<option>Lunch</option><option>Breakfast</option><option>Gym</option>"
        "<option>Class</option><option>Commute</option><option>Custom</option></select>"
        "<input type='time' name='protected_start'><input type='time' name='protected_end'>"
        "</div></div><button type='button' class='button-quiet' onclick='addProtectedRow()'>+ Add protected moment</button></fieldset>"
        "<details><summary>Testing / paste protected moments</summary>"
        "<p class='hint'>One per line: name | start | end.</p>"
        "<textarea name='protected_blocks' placeholder='Lunch | 13:00 | 14:00\nGym | 18:00 | 19:00'></textarea></details>"
        "<fieldset><legend>Tasks</legend><div id='task-rows'>"
        "<div class='entry-row task-row'><input name='task_title' placeholder='Task name'>"
        "<select name='task_minutes'><option value='30'>30 min</option><option value='60' selected>60 min</option><option value='90'>90 min</option><option value='120'>120 min</option><option value='150'>150 min</option></select>"
        "<select name='task_priority'><option value='1'>Low</option><option value='2'>2</option><option value='3' selected>Medium</option><option value='4'>4</option><option value='5'>High</option></select>"
        "<label class='deadline-choice'><input type='checkbox' checked onchange='toggleDeadline(this)'> No deadline</label>"
        "<input type='time' name='task_deadline'></div></div>"
        "<button type='button' class='button-quiet' onclick='addTaskRow()'>+ Add task</button></fieldset>"
        "<details><summary>Testing / legacy task format</summary>"
        "<p class='hint'>One task per line: title | minutes | priority | deadline. Use none when there is no deadline.</p>"
        "<textarea name='tasks' placeholder='Study networks | 90 | 3 | 18:00\nReply to email | 30 | 2 | none'></textarea></details>"
        "<button type='submit'>Generate schedule</button></form>"
        "<script>"
        "function toggleDeadline(box){const field=box.closest('.task-row').querySelector('[name=task_deadline]'); field.value=box.checked?'':field.value;}"
        "function addTaskRow(){const row=document.querySelector('.task-row').cloneNode(true); row.querySelector('[name=task_title]').value=''; row.querySelector('[name=task_title]').required=true; row.querySelector('[type=checkbox]').checked=true; row.querySelector('[name=task_deadline]').value=''; document.getElementById('task-rows').appendChild(row);}"
        "function addProtectedRow(){const row=document.querySelector('.protected-row').cloneNode(true); row.querySelectorAll('input').forEach(input=>input.value=''); row.querySelector('select').value=''; document.getElementById('protected-rows').appendChild(row);}"
        "</script>"
        "<p><a href='/'>Pending decisions</a></p>",
    )


@app.post("/schedule", response_class=HTMLResponse)
def create_schedule(
    request: Request,
    day: str = Form(...),
    available_start: str = Form(...),
    available_end: str = Form(...),
    morning_energy: int = Form(...),
    afternoon_energy: int = Form(...),
    evening_energy: int = Form(...),
    task_break_minutes: int = Form(15),
    protected_name: list[str] = Form([]),
    protected_start: list[str] = Form([]),
    protected_end: list[str] = Form([]),
    task_title: list[str] = Form([]),
    task_minutes: list[int] = Form([]),
    task_priority: list[int] = Form([]),
    task_deadline: list[str] = Form([]),
    protected_blocks: str = Form(""),
    tasks: str = Form(""),
):
    try:
        protected_lines = [
            f"{name or 'Protected'} | {start} | {end}"
            for name, start, end in zip(
                protected_name, protected_start, protected_end)
            if name and start and end
        ]
        protected_blocks = "\n".join(protected_lines) or protected_blocks
        protected = _protected_blocks(day, protected_blocks)
        profile = _profile(day, available_start, available_end,
                           morning_energy, afternoon_energy, evening_energy,
                           task_break_minutes=task_break_minutes,
                           protected_blocks=protected)
        if any(title.strip() for title in task_title):
            parsed_tasks = _task_rows(
                day, task_title, task_minutes, task_priority,
                task_deadline,
                available_start,
            )
        else:
            parsed_tasks = _tasks(day, tasks, available_start)
        run_id, _ = _run_schedule({
            "profile": profile.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in parsed_tasks],
            "existing_blocks": [],
        }, request.state.user_id)
        return _page("Schedule ready", _schedule_html(run_id))
    except (ValueError, TypeError) as exc:
        return _page("Schedule input error", f"<h1>Could not build that schedule</h1><p class='error'>{html.escape(str(exc))}</p><p><a href='/schedule'>Back to schedule form</a></p>")


@app.post("/runs/{run_id}/add-tasks", response_class=HTMLResponse)
def add_tasks(request: Request, run_id: str, tasks: str = Form(...)):
    store = _store()
    try:
        _require_owned(store, run_id, request.state.user_id)
        payload = store.latest(run_id, "input")
        schedule = store.latest(run_id, "proposed_schedule")
        if payload is None or schedule is None:
            raise ValueError("That schedule run no longer exists.")
        profile = UserScheduleProfile.model_validate(payload["profile"])
        existing_tasks = list(payload.get("tasks", []))
        day = profile.available_windows[0].start.date().isoformat()
        available_start = profile.available_windows[0].start.strftime("%H:%M")
        additions = _tasks(day, tasks, available_start)
        next_index = len(existing_tasks) + 1
        additions = [
            item.model_copy(update={"id": f"task-{next_index + index:03d}"})
            for index, item in enumerate(additions)
        ]
        next_payload = dict(payload)
        next_payload["tasks"] = existing_tasks + [
            item.model_dump(mode="json") for item in additions
        ]
        next_payload["existing_blocks"] = schedule.get("blocks", [])
        next_payload["added_task_ids"] = [item.id for item in additions]
        new_run_id, _ = _run_schedule(next_payload, request.state.user_id)
        return _page("Tasks added", _schedule_html(new_run_id))
    except (ValueError, TypeError) as exc:
        return _page(
            "Add tasks error",
            f"<h1>Could not add those tasks</h1><p class='error'>{html.escape(str(exc))}</p>"
            f"<p><a href='/runs/{html.escape(run_id)}'>Back to schedule</a></p>",
        )


@app.post("/runs/{run_id}/reschedule", response_class=HTMLResponse)
def reschedule(
    request: Request,
    run_id: str,
    task_id: str = Form(...),
    reason: str = Form(""),
    occurred_at: str | None = Form(None),
):
    store = _store()
    try:
        _require_owned(store, run_id, request.state.user_id)
        payload = store.latest(run_id, "input")
        schedule = store.latest(run_id, "proposed_schedule")
        if payload is None or schedule is None:
            raise ValueError("That schedule run no longer exists.")
        tasks = payload.get("tasks", [])
        if task_id not in {task.get("id") for task in tasks}:
            raise ValueError("That task is not part of this schedule.")
        next_payload = dict(payload)
        next_payload["existing_blocks"] = schedule.get("blocks", [])
        next_payload["missed_task_id"] = task_id
        next_payload["reason"] = reason.strip() or "The task was interrupted."
        next_payload["current_time"] = (
            datetime.fromisoformat(occurred_at).isoformat()
            if occurred_at else datetime.now(timezone.utc).isoformat()
        )
        next_payload["rescheduled_task_id"] = task_id
        next_payload["rescheduled_from_run_id"] = run_id
        next_payload["previous_schedule"] = schedule
        new_run_id, _ = _run_schedule(next_payload, request.state.user_id)
        return _page("Schedule updated", _schedule_html(new_run_id))
    except (KeyError, ValueError, TypeError) as exc:
        return _page("Reschedule error", f"<h1>Could not reschedule that task</h1><p class='error'>{html.escape(str(exc))}</p><p><a href='/schedule'>Create a new schedule</a></p>")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def show_run(request: Request, run_id: str):
    try:
        store = _store()
        _require_owned(store, run_id, request.state.user_id)
        store.get_state(run_id)
    except KeyError:
        return _page("Not found", "<h1>Not found</h1><p class='sub'>No such schedule run.</p>")
    return _page("Schedule updated", _schedule_html(run_id))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    s = _store()
    callback.sweep(s)
    user_id = request.state.user_id
    open_qs = [
        question for question in callback.pending(s)
        if _owned_run(s, question.run_id, user_id)
    ]
    if not open_qs:
        return _page("All clear", "<h1>All clear</h1><p class='sub'>NANI isn't stuck on anything right now.</p><p><a href='/schedule'>Build a schedule</a></p>")
    items = "".join(
        f"<div class='card'><p class='q'>{html.escape(q.question)}</p><a href='/q/{q.id}'>Sort this out &rarr;</a></div>" for q in open_qs)
    return _page("NANI needs a decision", f"<h1>{len(open_qs)} thing(s) NANI can't decide alone</h1>" + items)

    parsed = [
        {
            "id": task["id"],
            "title": task["title"],
            "description": task.get("description"),
            "deadline": task.get("deadline"),
            "estimated_minutes": task["estimated_duration"],
            "priority": task.get("priority", "medium"),
            "status": "pending",
        }
        for task in payload.get("tasks", [])
        if task.get("id") not in scheduled_ids
    ]
    q = _store().get_question(qid)
    if q is None:
        return _page("Not found", "<h1>Not found</h1><p class='sub'>No such question.</p>")
    if q.is_answered:
        return _page("Already sorted", f"<h1>Already sorted</h1><div class='ctx'><b>What you said</b>{html.escape(q.answer or '')}</div><p><a href='/'>Back</a></p>")
    return _page("A call only you can make",
                 f"<h1>A call only you can make</h1>"
                 f"<p class='q'>{html.escape(q.question)}</p>"
                 f"<form method='post' action='/q/{q.id}'>"
                 "<textarea name='answer' autofocus placeholder='What should NANI do&hellip;'></textarea>"
                 "<input type='hidden' name='who' value='user'>"
                 "<button type='submit'>Send</button></form>")


@app.get("/q/{qid}", response_class=HTMLResponse)
def show_question(request: Request, qid: str):
    store = _store()
    question = store.get_question(qid)
    if question is not None and not _owned_run(store, question.run_id, request.state.user_id):
        question = None
    if question is None:
        raise HTTPException(status_code=404, detail="Not found")
    if question.is_answered:
        return _page(
            "Already answered",
            "<h1>Already answered</h1>"
            f"<div class='ctx'><b>Your answer</b>{html.escape(question.answer or '')}</div>"
            "<p><a href='/'>Back</a></p>",
        )
    context_html = ""
    conflicts = question.context.get("conflicts", [])
    if conflicts:
        context_html = "<div class='conflict-list'>" + "".join(
            "<div class='conflict-item'>"
            f"<strong>{html.escape(str(item.get('conflict_type', 'conflict')).replace('_', ' ').title())}</strong>"
            f"<span>{html.escape(item.get('explanation', 'No safe slot was found.'))}</span></div>"
            for item in conflicts
        ) + "</div>"
    return _page(
        "Human decision",
        "<h1>A decision is needed</h1>"
        f"<p class='q'>{html.escape(question.question)}</p>"
        f"{context_html}"
        f"<form method='post' action='/q/{html.escape(qid)}'>"
        "<textarea name='answer' autofocus placeholder='Tell Nagare what to do...'></textarea>"
        "<input type='hidden' name='who' value='user'>"
        "<button type='submit'>Send decision</button></form>"
        "<p class='note'>Your answer is recorded and used to resume this run.</p>",
    )


@app.post("/q/{qid}")
def submit(request: Request, qid: str, answer: str = Form(...), who: str = Form("user")):
    text = (answer or "").strip()
    if not text:
        return RedirectResponse(f"/q/{qid}", status_code=303)
    store = _store()
    question = store.get_question(qid)
    if question is None or not _owned_run(store, question.run_id, request.state.user_id):
        raise HTTPException(status_code=404, detail="Not found")
    run_id = callback.answer(store, qid, text, who=request.state.user_id)
    if run_id:
        current_settings = load_settings()
        call = _focus_model_call(current_settings, request.state.user_id) if store.get_domain(
            run_id) == "nagare_focus" else None
        if store.get_domain(run_id) == "nagare_focus":
            runner.advance(store, run_id, build_focus_flow(
                call), current_settings)
            return RedirectResponse(f"/focus/runs/{run_id}", status_code=303)
        if os.environ.get("NAGARE_AGENT_MODE", "model").lower() == "model" and current_settings.api_key:
            from slice.llm import complete
            call = complete
        runner.advance(store, run_id, build_flow(call), current_settings)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    return RedirectResponse("/thanks", status_code=303)


@app.get("/thanks", response_class=HTMLResponse)
def thanks():
    return _page("Got it", "<h1>Got it</h1><p class='sub'>NANI has resumed planning with your answer.</p><p><a href='/'>Anything else waiting?</a></p>")

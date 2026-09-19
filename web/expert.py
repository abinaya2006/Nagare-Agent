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
import os
from datetime import date, datetime, time, timedelta, timezone

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from demo.nagare.flow import build_flow
from demo.nagare.schema import CircadianProfile, Task, TimeWindow, UserScheduleProfile
from slice import callback, runner
from slice.config import settings as load_settings
from slice.records import RunState
from slice.store import Store

DB = os.environ.get("SLICE_DB", "run.db")
app = FastAPI(title="Nagare — schedule check-in")


def _store() -> Store:
    return Store(DB)


def _datetime(day: str, value: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(value))


def _profile(day: str, available_start: str, available_end: str,
             morning_energy: int, afternoon_energy: int,
             evening_energy: int) -> UserScheduleProfile:

    start_today = _datetime(day, available_start)
    end_today = _datetime(day, available_end)
    noon_today = _datetime(day, "12:00")
    peak_end_today = min(
        end_today, noon_today) if start_today < noon_today else end_today

    # CRITICAL FIX: Give the AI mathematical space for "Tomorrow"
    start_tomorrow = start_today + timedelta(days=1)
    end_tomorrow = end_today + timedelta(days=1)
    peak_end_tomorrow = peak_end_today + timedelta(days=1)

    return UserScheduleProfile(
        available_windows=[
            TimeWindow(start=start_today, end=end_today),
            TimeWindow(start=start_tomorrow, end=end_tomorrow)
        ],
        circadian_profile=CircadianProfile(
            morning_energy=morning_energy,
            afternoon_energy=afternoon_energy,
            evening_energy=evening_energy,
            peak_periods=[
                TimeWindow(start=start_today, end=peak_end_today),
                TimeWindow(start=start_tomorrow, end=peak_end_tomorrow)
            ],
        ),
        preferred_work_periods=[
            TimeWindow(start=start_today, end=peak_end_today),
            TimeWindow(start=start_tomorrow, end=peak_end_tomorrow)
        ],
        protected_blocks=[],
        preferred_session_length=60,
        preferred_break_length=15,
        sleep_window=TimeWindow(
            start=_datetime(day, "23:00"),
            end=_datetime(day, "23:00") + timedelta(hours=8),
        ),
        commute_windows=[],
    )


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
        deadline = _datetime(day, parts[3]) if len(
            parts) > 3 and parts[3] else None

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


def _run_schedule(payload: dict) -> tuple[str, RunState]:
    store = _store()
    run_id = store.create_run("nagare")
    store.append(run_id, "input", payload, produced_by="web_user")
    settings = load_settings()
    call = None
    if os.environ.get("NAGARE_AGENT_MODE", "offline").lower() == "model" and settings.api_key:
        from slice.llm import complete
        call = complete
    return run_id, runner.advance(store, run_id, build_flow(call), settings)

# --- (The rest of the UI rendering code in web/expert.py remains identical) ---


def _schedule_html(run_id: str) -> str:
    store = _store()
    state = store.get_state(run_id)
    input_data = store.latest(run_id, "input") or {}
    schedule = store.latest(run_id, "proposed_schedule") or {}
    failure = store.latest(run_id, "failure")
    tasks = {task["id"]: task for task in input_data.get("tasks", [])}

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(block['start'][8:16].replace('T', ' '))} - {html.escape(block['end'][11:16])}</td>"
        f"<td>{html.escape(tasks.get(block.get('task_id'), {}).get('title', block.get('block_type', 'block')))}</td>"
        f"<td>{'Locked' if block.get('locked') else 'Movable'}</td></tr>"
        for block in schedule.get("blocks", [])
    ) or "<tr><td colspan='3'>No task could be placed in the available windows.</td></tr>"

    options = "".join(
        f"<option value='{html.escape(task_id)}'>{html.escape(task['title'])}</option>"
        for task_id, task in tasks.items()
    )

    pending = callback.pending(store, run_id)
    decision = ""
    if pending:
        question = pending[0]
        decision = (
            "<div class='card'><h2>Your decision is needed</h2>"
            f"<p class='sub'>{html.escape(question.question)}</p>"
            f"<a class='nav-link' href='/q/{html.escape(question.id)}'>Answer this decision</a></div>"
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
<div class="shell"><header class="masthead"><a class="brand" href="/"><span class="brand-mark">N</span><span>NAGARE<small>daily flow planner</small></span></a><a class="nav-link" href="/schedule">New schedule +</a></header>{body}<p class="footer">Nagare plans around your constraints, not against them.</p></div>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body))


@app.get("/schedule", response_class=HTMLResponse)
def schedule_form():
    return _page(
        "Build your schedule",
        "<h1>Build your schedule</h1>"
        "<p class='sub'>Tell Nagare when you are available and what needs to get done.</p>"
        "<form method='post' action='/schedule'>"
        f"<label>Date<input type='date' name='day' value='{datetime.now(timezone.utc).date().isoformat()}' required></label>"
        "<label>Available from<input type='time' name='available_start' value='09:00' required></label>"
        "<label>Available until<input type='time' name='available_end' value='21:00' required></label>"
        "<label>Morning energy (0-5)<input type='number' name='morning_energy' min='0' max='5' value='5' required></label>"
        "<label>Afternoon energy (0-5)<input type='number' name='afternoon_energy' min='0' max='5' value='3' required></label>"
        "<label>Evening energy (0-5)<input type='number' name='evening_energy' min='0' max='5' value='2' required></label>"
        "<label>Tasks<textarea name='tasks' required placeholder='Task title | minutes | priority | deadline\nStudy networks | 90 | 3 | 18:00\nReply to email | 30 | 2'></textarea></label>"
        "<p class='hint'>One task per line. Deadline is optional and uses HH:MM. Priority is 1 (low) to 5 (high).</p>"
        "<button type='submit'>Generate schedule</button></form>"
        "<p><a href='/'>Pending decisions</a></p>",
    )


@app.post("/schedule", response_class=HTMLResponse)
def create_schedule(
    day: str = Form(...),
    available_start: str = Form(...),
    available_end: str = Form(...),
    morning_energy: int = Form(...),
    afternoon_energy: int = Form(...),
    evening_energy: int = Form(...),
    tasks: str = Form(...),
):
    try:
        profile = _profile(day, available_start, available_end,
                           morning_energy, afternoon_energy, evening_energy)
        parsed_tasks = _tasks(day, tasks, available_start)
        run_id, _ = _run_schedule({
            "profile": profile.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in parsed_tasks],
            "existing_blocks": [],
        })
        return _page("Schedule ready", _schedule_html(run_id))
    except (ValueError, TypeError) as exc:
        return _page("Schedule input error", f"<h1>Could not build that schedule</h1><p class='error'>{html.escape(str(exc))}</p><p><a href='/schedule'>Back to schedule form</a></p>")


@app.post("/runs/{run_id}/reschedule", response_class=HTMLResponse)
def reschedule(run_id: str, task_id: str = Form(...), reason: str = Form("")):
    store = _store()
    try:
        payload = store.latest(run_id, "input")
        schedule = store.latest(run_id, "proposed_schedule")
        if payload is None or schedule is None:
            raise ValueError("That schedule run no longer exists.")
        payload["existing_blocks"] = schedule.get("blocks", [])
        payload["missed_task_id"] = task_id
        payload["reason"] = reason.strip() or "The task was interrupted."
        new_run_id, _ = _run_schedule(payload)
        return _page("Schedule updated", _schedule_html(new_run_id))
    except (KeyError, ValueError, TypeError) as exc:
        return _page("Reschedule error", f"<h1>Could not reschedule that task</h1><p class='error'>{html.escape(str(exc))}</p><p><a href='/schedule'>Create a new schedule</a></p>")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def show_run(run_id: str):
    try:
        _store().get_state(run_id)
    except KeyError:
        return _page("Not found", "<h1>Not found</h1><p class='sub'>No such schedule run.</p>")
    return _page("Schedule updated", _schedule_html(run_id))


@app.get("/", response_class=HTMLResponse)
def index():
    s = _store()
    callback.sweep(s)
    open_qs = callback.pending(s)
    if not open_qs:
        return _page("All clear", "<h1>All clear</h1><p class='sub'>NANI isn't stuck on anything right now.</p><p><a href='/schedule'>Build a schedule</a></p>")
    items = "".join(
        f"<div class='card'><p class='q'>{html.escape(q.question)}</p><a href='/q/{q.id}'>Sort this out &rarr;</a></div>" for q in open_qs)
    return _page("NANI needs a decision", f"<h1>{len(open_qs)} thing(s) NANI can't decide alone</h1>" + items)


@app.get("/q/{qid}", response_class=HTMLResponse)
def show(qid: str):
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


@app.post("/q/{qid}")
def submit(qid: str, answer: str = Form(...), who: str = Form("user")):
    text = (answer or "").strip()
    if not text:
        return RedirectResponse(f"/q/{qid}", status_code=303)
    store = _store()
    run_id = callback.answer(store, qid, text, who=who)
    if run_id:
        current_settings = load_settings()
        call = None
        if os.environ.get("NAGARE_AGENT_MODE", "offline").lower() == "model" and current_settings.api_key:
            from slice.llm import complete
            call = complete
        runner.advance(store, run_id, build_flow(call), current_settings)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    return RedirectResponse("/thanks", status_code=303)


@app.get("/thanks", response_class=HTMLResponse)
def thanks():
    return _page("Got it", "<h1>Got it</h1><p class='sub'>NANI has resumed planning with your answer.</p><p><a href='/'>Anything else waiting?</a></p>")

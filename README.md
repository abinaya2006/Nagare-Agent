# Nagare

Nagare is a scheduling and rescheduling agent built on top of the reusable Agentic Slice Kit engine. The product focus is simple:

- the user provides tasks, constraints, preferences, and their schedule;
- Nagare builds a baseline plan;
- if something changes, Nagare finds a workable reschedule;
- if no safe automatic decision exists, Nagare asks the user through the human-in-the-loop flow.

The important product idea is not “perfect AI scheduling from scratch.” It is: “the user tells us what changed, and Nagare rearranges the schedule while preserving the important constraints.”

---

## What Nagare does

Nagare is designed to handle:

- task scheduling from a user’s availability and protected time
- deadline-aware planning
- conflict detection
- rescheduling when a task is unfinished or disrupted
- validation of the proposed schedule before accepting it
- escalation to a human only when the system cannot decide safely

This is a rule-based scheduling agent with an explicit validation gate and a human back-edge.

Nagare also includes a selected-task focus workflow. The user chooses one task
from their pending queue, Nagare recommends one manageable focus block, and the
user accepts, rejects, or skips it. Rejection reasons and breakdown decisions
are stored across runs so later recommendations can adapt. See
[docs/NAGARE-FOCUS-SPEC.md](docs/NAGARE-FOCUS-SPEC.md).

---

## Core workflow

The Nagare flow is:

1. Load user data and task list
2. Build a baseline schedule
3. Detect conflicts or missing work
4. Reschedule the affected task
5. Validate the candidate schedule
6. Retry if needed
7. Ask the user only when the choice depends on a preference trade-off

The lifecycle is intentionally deterministic:

- planner proposes a schedule
- validator checks it independently
- runner coordinates the state change
- callback layer asks a user if necessary

---

## Repository structure

```text
slice/                Reusable agent infrastructure
  config.py           settings and environment
  records.py          state and records
  store.py            SQLite-backed durable state
  budget.py           tokens and attempt fences
  llm.py              model boundary
  callback.py         suspend / resume / human answer flow
  runner.py           state-machine orchestration

web/                  Human-interaction UI
  expert.py           pending-question pages and answer capture

demo/
  nagare/             Nagare-specific scheduling logic
    schema.py         data models for tasks, windows, blocks, conflicts
    planner.py        deterministic scheduling and rescheduling
    validator.py      independent schedule gate
    flow.py           Nagare state machine and escalation behavior
    demo_data.py      sample schedule data
    NAGARE-SPEC.md    product spec for the scheduling agent
  terminal.py         interactive terminal CLI for running Nagare and handling user decisions
  smoke/              reference demo used to understand the engine pattern

tests/
  test_nagare.py      Nagare planner + validator checks
  test_runner.py      runner state transitions
  test_callback.py    human callback behavior
  test_smoke.py       reference smoke flow

scripts/
  doctor.py           repo health check
```

---

## Nagare scheduling logic

### Planner
The planner applies deterministic rules such as:

- fixed tasks first
- earlier deadlines and higher priority next
- available windows only
- protected blocks and sleep excluded
- no overlap with already placed blocks

### Validator
The validator is independent from the planner and enforces:

- no time overlaps
- no protected-time violations
- no sleep violations
- no deadline violations
- no availability violations
- no mutation of locked blocks

### Human-in-the-loop escalation
If no valid automatic schedule can be found, Nagare creates a question and sends the user to the callback flow in [web/expert.py](web/expert.py). The answer resumes the run and continues from the saved state.

---

## Quick start

Install dependencies:

```bash
pip install -r requirements.txt
```

### Terminal implementation flow

The terminal-based implementation is the simplest way to exercise the project in practice. The real CLI entry point lives in [demo/terminal.py](demo/terminal.py).

To launch the interactive version directly:

```bash
python demo/terminal.py
```

Useful one-off runs are also available:

```bash
python demo/terminal.py --run
python demo/terminal.py --miss task-001
python demo/terminal.py --pending
python demo/terminal.py --replay <RUN_ID>
python demo/terminal.py --focus
```

1. Start the human-answer app:

```bash
uvicorn web.expert:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/focus` to enter pending tasks, select one, and run
the Focus Agent workflow. The existing schedule workflow remains at
`http://localhost:8000/schedule`.

2. In another terminal, run the scheduler or trigger a run from the repo logic:

```bash
python - <<'PY'
from slice.store import Store
from slice.config import settings
from slice import runner
from demo.nagare.flow import build_flow

store = Store('run.db')
run_id = store.create_run('nagare')
store.append(run_id, 'input', {
    'tasks': [
        {
            'id': 'ml',
            'title': 'ML assignment',
            'estimated_duration': 120,
            'priority': 5,
            'consequence_of_delay': 5,
            'deadline_type': 'hard',
            'movable': True,
            'fixed': False,
            'earliest_start': '2026-09-19T09:00:00',
            'latest_finish': '2026-09-19T18:00:00',
            'deadline': '2026-09-19T18:00:00',
        }
    ],
    'profile': {
        'available_windows': [
            {'start': '2026-09-19T09:00:00', 'end': '2026-09-19T17:00:00'}
        ],
        'circadian_profile': {
            'morning_energy': 5,
            'afternoon_energy': 3,
            'evening_energy': 2,
            'peak_periods': [{'start': '2026-09-19T09:00:00', 'end': '2026-09-19T12:00:00'}]
        },
        'preferred_work_periods': [
            {'start': '2026-09-19T09:00:00', 'end': '2026-09-19T12:00:00'}
        ],
        'protected_blocks': [],
        'preferred_session_length': 60,
        'preferred_break_length': 30,
        'sleep_window': {'start': '2026-09-19T23:00:00', 'end': '2026-09-20T07:00:00'},
        'commute_windows': []
    },
    'existing_blocks': []
}, produced_by='system')

final_state = runner.advance(store, run_id, build_flow(), settings)
print(final_state)
print(store.replay(run_id)[:5])
PY
```

3. Open the question page in a browser:

```text
http://localhost:8000/
```

4. Submit the user answer to resume the run:

```text
http://localhost:8000/q/<QUESTION_ID>
```

5. The run will resume after the answer is posted and persisted.

### Basic browser UI

The FastAPI app also includes a basic user intake page at [http://localhost:8000/schedule](http://localhost:8000/schedule). The user enters available time, energy levels, and tasks using `title | minutes | priority | deadline`. Nagare generates the schedule, displays the result, and provides a reschedule form for a missed task. Rescheduling creates a new persisted run using the previous schedule as occupied context.

### Test commands

Run the focused Nagare tests:

```bash
pytest -q tests/test_nagare.py
```

Run the runner + callback integration checks:

```bash
pytest -q tests/test_runner.py tests/test_callback.py tests/test_nagare.py
```

Run the full suite:

```bash
pytest -q
```

Run the repo health checker:

```bash
python scripts/doctor.py
```

---

## Verified status

Current repository verification:

- Nagare-focused tests: passing
- runner + callback integration: passing
- full test suite: passing
- repo health check: all clear

This was validated with the current workspace commands, including:

```bash
pytest -q tests/test_nagare.py
pytest -q tests/test_runner.py tests/test_callback.py tests/test_nagare.py
pytest -q
python scripts/doctor.py
```

---

## Current project emphasis

Nagare is intentionally built as a strong MVP scheduling agent rather than a massive optimization engine. The current implementation favors:

- deterministic rules for the core schedule logic
- validation before accepting a plan
- bounded retry logic
- explicit user input only when a preference trade-off is unavoidable

This keeps the system auditable, explainable, and easy to extend.

---

## Notes

This repo started as the Agentic Slice Kit scaffold, but the current product is the Nagare scheduling demo layered on top of the reusable slice engine. The reusable infrastructure stays in [slice](slice), and all product-specific scheduling behavior lives under [demo/nagare](demo/nagare).

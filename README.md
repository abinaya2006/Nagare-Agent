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

Start the human question page:

```bash
uvicorn web.expert:app --host 0.0.0.0 --port 8000
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

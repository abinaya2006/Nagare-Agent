# Project Report: Nagare Agent

Date: 2026-09-19 

## 1. Goal

Nagare is being built as a scheduling and rescheduling agent for real-world daily plans. The core idea is:

- the user provides the schedule constraints, tasks, and preferences;
- Nagare builds a baseline schedule;
- if reality changes, Nagare reschedules the affected task;
- if a safe choice still cannot be made, Nagare asks the user through the human-in-the-loop callback flow.

The main product focus is rescheduling, not building a large AI optimization engine from scratch.

---

## 2. Repo architecture and purpose

### Core reusable engine: [slice](../slice)

The [slice](../slice) directory contains the reusable state-machine infrastructure. It is the engine layer and should stay general-purpose.

Key files:

- [slice/config.py](../slice/config.py): settings and environment configuration
- [slice/records.py](../slice/records.py): state and record definitions
- [slice/store.py](../slice/store.py): append-only SQLite-backed durable state
- [slice/runner.py](../slice/runner.py): state-machine runner and loop controls
- [slice/callback.py](../slice/callback.py): human-in-the-loop suspend/resume logic
- [slice/budget.py](../slice/budget.py): token and attempt budgets
- [slice/llm.py](../slice/llm.py): model-call boundary

### Product-specific logic: [demo](../demo)

The [demo](../demo) folder holds the domain-specific flows and knowledge. This is where Nagare was implemented.

Key files:

- [demo/nagare/schema.py](../demo/nagare/schema.py): Nagare task, schedule, profile, conflict, and decision models
- [demo/nagare/planner.py](../demo/nagare/planner.py): scheduling and rescheduling logic
- [demo/nagare/validator.py](../demo/nagare/validator.py): independent gate/validation layer
- [demo/nagare/flow.py](../demo/nagare/flow.py): Nagare agent run loop
- [demo/nagare/demo_data.py](../demo/nagare/demo_data.py): sample schedule and task data
- [demo/smoke](../demo/smoke): working reference pattern used to understand the repo’s state-machine structure

### Human input surface: [web/expert.py](../web/expert.py)

[web/expert.py](../web/expert.py) is the human-facing web layer for queries that require user input.

Features:

- `GET /` shows pending questions
- `GET /q/{qid}` shows the specific question and context
- `POST /q/{qid}` records the user answer
- `callback.answer(...)` resumes the run from persisted state

This matches the repo’s human-in-the-loop pattern.

---

## 3. Workflow implemented for Nagare

The actual Nagare flow is a deterministic agentic loop built around the existing slice runner.

### Flow stages

1. Input is loaded from the run context
   - tasks
   - schedule profile
   - availability windows
   - protected/sleep blocks
   - existing scheduled blocks

2. Drafting stage
   - build a candidate schedule or reschedule the missed task
   - apply simple rule-based scheduling logic
   - record a `proposed_schedule`
   - attach a `reschedule_attempt`

3. Gating stage
   - validate the proposed schedule independently
   - produce `validation` output with conflicts when applicable

4. Decision path
   - if validation passes: mark the run as `COMPLETE`
   - if validation fails and retry logic is exhausted: ask the user via the callback mechanism
   - if a user answer is given, resume with the workflow and continue from the stored state

### State pattern

The runner uses the repo’s durable state machine:

- `DRAFTING`
- `GATING`
- `AWAITING_EXPERT`
- `COMPLETE`
- `FAILED`

This is the proper agentic pattern used across the slice engine.

---

## 4. Core Nagare logic built today

### Scheduling models

Implemented in [demo/nagare/schema.py](../demo/nagare/schema.py):

- `TimeWindow`
- `CircadianProfile`
- `UserScheduleProfile`
- `ScheduleBlock`
- `Task`
- `Conflict`
- `RescheduleAttempt`
- `ProposedSchedule`
- `ScheduleValidationResult`
- `SchedulingDecision`

These models capture tasks, protected time, sleep, preferences, schedule blocks, and validation outcomes.

### Planner

Implemented in [demo/nagare/planner.py](../demo/nagare/planner.py):

- candidate slot search
- baseline schedule generation
- protected time filtering
- availability filtering
- task ordering by fixed/deadline/priority/consequence
- simple deterministic rescheduling using the earliest feasible slot

### Validator

Implemented in [demo/nagare/validator.py](../demo/nagare/validator.py):

- time-overlap checks
- deadline checks
- availability checks
- protected-block checks
- locked-block change detection
- schedule status enforcement (`PASS` / `BLOCK`)

### Flow

Implemented in [demo/nagare/flow.py](../demo/nagare/flow.py):

- deterministic `build_flow()` state machine
- planner + validator integration
- human escalation when the schedule remains unsafe or unresolved
- forced retry loop using maximum revision limits
- resume from `expert_answer` when a user replies

---

## 5. Tests created and run

### Nagare-specific tests

Created in [tests/test_nagare.py](../tests/test_nagare.py)

Covered behaviors:

- baseline schedule prioritization
- protected-time exclusion
- rescheduling into a feasible future slot
- preserving locked/protected blocks
- valid schedule validation
- overlap detection
- locked-block mutation detection
- invalid deadline field rejection

### Additional relevant tests

The repo already has runner and callback tests covering the agent loop and the human-in-the-loop contract:

- [tests/test_runner.py](../tests/test_runner.py)
- [tests/test_callback.py](../tests/test_callback.py)
- [tests/test_smoke.py](../tests/test_smoke.py)

---

## 6. Verification evidence

### Focused Nagare verification

Command run:

```bash
cd /workspaces/Nagare-Agent && pytest -q tests/test_nagare.py
```

Result:

```text
8 passed in 0.11s
```

### Runner + callback + Nagare integration check

Command run:

```bash
cd /workspaces/Nagare-Agent && pytest -q tests/test_runner.py tests/test_callback.py tests/test_nagare.py
```

Result:

```text
18 passed in 0.34s
```

### Full repository validation

Command run:

```bash
cd /workspaces/Nagare-Agent && pytest -q
```

Result:

```text
82 passed in 12.26s
```

### Repository health check

Command run:

```bash
cd /workspaces/Nagare-Agent && python scripts/doctor.py
```

Result:

```text
all clear
```

---

## 7. What was fixed today

Important corrections completed during the day:

1. Clarified repo split: reusable engine vs. product-specific logic
2. Confirmed the correct human-in-the-loop pattern in [web/expert.py](../web/expert.py)
3. Fixed Nagare model constraints and schedule-block validation logic
4. Built a deterministic rule-based planner and validator for Nagare
5. Added focused tests for Nagare scheduling behavior
6. Verified the agentic loop through the runner and callback path

---

## 8. Current status

Nagare is in a solid MVP state for its scheduling engine:

- deterministic task scheduling works
- validation logic works
- rescheduling behavior is in place
- human escalation is wired into the runner and callback flow
- tests validate the expected behavior

### Still intentionally not fully complete

The current implementation is not yet a full production scheduling product. Remaining priorities include:

- richer natural-language task intake
- more advanced conflict resolution logic
- broader schedule rules (commute, fragmentation, break logic, deeper preference handling)
- more complete UI/data entry flow for a user to paste an entire schedule profile

That said, the foundation is stable and the agentic loop is functioning correctly.

---

## 9. Summary

Today’s work established the core Nagare product architecture, implemented the rule-based scheduling backbone, validated it independently, connected it to the existing runner/human loop, and verified the result with passing tests and repo health checks.

The project is now at a state where the scheduling agent can be extended further without breaking the core loop.

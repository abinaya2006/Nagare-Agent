# Nagare Stress Test

**Tester:** Suganthi
**Evidence:** https://drive.google.com/drive/folders/1NjTPehIUYsCJgTO8oNjopnpjuMCECEUp?usp=drive_link

## Purpose

Exercise scheduling, validation, human checkpoints, rescheduling, focus
progress, and API fallback with deliberately difficult inputs. The test checks
that Nagare does not silently accept an incomplete or impossible schedule.

## Scenarios

### Past-time scheduling

Use a same-day schedule after the availability window has begun.

Expected behavior:

- The initial schedule respects the configured availability window.
- Add-task and reschedule operations do not place new work before the current time.
- A completed past task is not silently recreated without a user action.

This was a previous failure: the schedule began at the initial available time
regardless of when the request was made, which allowed past scheduling.

### Deadline and priority pressure

```text
assignment | 60 | 4 | 18:00
shopping | 150 | 3 | 17:00
leetcode | 100 | 4 | 14:00
```

Expected behavior:

- Hard deadlines remain feasibility constraints.
- Among feasible tasks, higher priority is preferred.
- Earlier deadline breaks equal-priority ties.
- A task that cannot fit is reported as a conflict instead of disappearing.

### Protected-time fragmentation

```text
breakfast | 10:00 | 10:30
lunch | 12:00 | 13:00
dinner | 20:00 | 21:00
```

Expected behavior:

- Protected moments and sleep are never used for task blocks.
- **Split recommended task** creates fragments and replans the remaining tasks
	around those fragments.
- Any unplaced remainder appears in the pending queue.
- A second human checkpoint is created only when a new conflict remains.

### Rescheduling

Select a scheduled task and submit **Reschedule task** after an interruption.

Expected behavior:

- The original block is shown as replaced in the UI.
- The replacement is placed in a future feasible slot, not the old slot.
- Movable tasks are replanned together, so a higher-priority task can move ahead
	of a lower-priority task.
- Locked and protected blocks remain unchanged.

This was a previous failure: rescheduling produced confusing intervals and did not consistently apply a human instruction to move the task.

### Focus progress

Start a focus block for a task longer than the recommendation, then record a
partial outcome.

Expected behavior:

- Consumed focus minutes are deducted from the pending queue.
- A 90-minute task with a 30-minute focus block shows 60 minutes remaining.
- A fully completed task is removed from the queue.

### API and offline behavior

Deterministic suite:

```bash
NAGARE_AGENT_MODE=offline pytest -q
```

Browser demo with AI explicitly enabled:

```bash
export NAGARE_AGENT_MODE=model
uvicorn web.expert:app --host 0.0.0.0 --port 8000
```

Expected behavior:

- Offline mode produces a deterministic schedule without an API call.
- API mode validates every model proposal.
- Empty or incomplete model proposals fall back to the deterministic schedule
	for tasks that can be placed, while genuine conflicts go to HITL.
- Slow model calls fall back after the web timeout instead of holding the
	browser request indefinitely.

## Validation commands

```bash
NAGARE_AGENT_MODE=offline pytest -q
python scripts/doctor.py
```

The suite covers planner ordering, hard deadlines, protected time,
fragmentation, rescheduling, current-time guards, focus remainder tracking,
structured and legacy input, and API fallback behavior.


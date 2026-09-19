# Nagare Focus Agent

## Goal

Help a student decide how to begin the task they selected from the pending queue, reduce cognitive overload, and preserve the student's control over every decision.

## Agent Boundary

Nagare does not need to choose among every pending task autonomously. The user selects the task they are considering. Nagare then recommends exactly one focus block or one concrete next action.

The existing scheduling and deadline-verification flow remains a separate behavior of the same Nagare product. When unfinished work reaches a deadline, the user decides whether to move, split, defer to the next day, or keep it pending.

## Reads

- selected pending task and its metadata
- optional task description, deadline, priority, and estimated effort
- current context supplied by the user
- recommendation decisions
- rejection reasons
- breakdown decisions
- recent outcomes

## Writes

Focus runs use the shared append-only SQLite store and callback mechanism. They write:

- `focus_recommendation`
- `focus_decision`
- `rejection_event`
- `breakdown_action`
- `breakdown_decision`
- `focus_outcome`
- `focus_state`
- `manual_control`

## Human Checkpoints

1. Nagare recommends one focus block. The user accepts, rejects, or skips it.
2. A rejection opens a reason question before replanning.
3. An overwhelming or vague task produces one 5-30 minute breakdown action. The user accepts or rejects that action.
4. After repeated failed replans, Nagare returns control to the user instead of nagging indefinitely.

## Learning

Focus events are persisted across runs under the `nagare_focus` domain. Later recommendations read that history. Repeated rejection of long blocks biases the fallback recommendation toward a shorter block. A history of breakdown actions makes the recommendation explanation acknowledge that smaller steps have worked better.

The live model receives the selected task and its relevant history as structured context. If the model is unavailable, returns the wrong task, or fails its typed contract, the deterministic fallback remains available.

## State Path

```text
selected task
  -> drafting
  -> focus recommendation
  -> awaiting human
  -> accepted / skipped / rejected
  -> working or asking rejection reason
  -> learning
  -> breakdown or replanning
  -> awaiting human again
  -> outcome / manual control
```

## Success Criterion

A user can enter several pending tasks, select one, accept or reject Nagare's recommendation, explain a rejection, approve a smaller next step, and see a later recommendation influenced by the stored history.

## Entry Points

- Browser: `GET /focus`, then select a task and answer the callback question.
- Terminal: `python demo/terminal.py --focus`.

The schedule/reschedule flow remains available at `/schedule` and through the existing terminal commands.

# Nagare Agentic Workflow

Nagare uses a bounded agentic loop for schedule planning. The model is allowed
to propose a typed schedule, but it does not control state transitions or the
final decision.

## Control Loop

1. `DRAFTING` assembles the user's tasks, energy profile, availability,
	existing blocks, previous proposal, validation conflicts, and human answer.
2. The planner agent returns a `ProposedSchedule` through the shared typed LLM
	boundary.
3. `GATING` runs the independent deterministic validator.
4. A passing schedule becomes a recorded decision and the run completes.
5. A blocked schedule returns to `DRAFTING` for a bounded number of revisions.
6. Repeated or unresolved conflicts create a durable callback question and move
	the run to `AWAITING_EXPERT`.
7. The user's answer is appended to the event log, the runner resumes, and an
	explicit deferral such as "move the task to tomorrow" becomes a recorded
	scheduling decision.

## Boundaries

- The model proposes; it cannot bypass validation.
- The validator checks overlap, availability, protected time, deadlines, sleep,
  locked blocks, and tasks omitted from the proposal.
- The runner owns sequencing, retry limits, persistence, and suspension.
- The callback layer owns human decisions and resume behavior.

## Execution Modes

Offline mode is the default for tests and local demos. It uses the existing
deterministic planner without a network call. To enable the model planner in
the browser app, configure an API key and set:

```bash
export NAGARE_AGENT_MODE=model
uvicorn web.expert:app --host 0.0.0.0 --port 8000
```

The terminal interface automatically uses the model planner when an API key is
available. In both modes, validation and human escalation remain deterministic
and auditable.

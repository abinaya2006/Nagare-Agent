# Design Rationale

## What it does

Nagare creates a daily schedule from tasks, available time, deadlines, and protected moments. When it cannot safely decide, it asks the user and records the answer before continuing.

## Why this shape

We chose not to let the model control validation, state changes, or protected-time rules because those decisions must be predictable and auditable. We also did not build a fully automatic rescheduler because some conflicts require the user's judgement.

## What it can't do

Nagare cannot solve a conflict when there is not enough valid time, when the user's answer does not resolve the conflict, or when a task requires information the system does not have. It also cannot safely invent a new time outside the user's available windows.

## What we'd do next

We would improve the interface for explaining conflicts and add more scheduling choices without weakening the deterministic checks. We would also test the system with more real schedules and edge cases before adding more automation.
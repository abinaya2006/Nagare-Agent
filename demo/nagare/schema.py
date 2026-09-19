"""Pydantic schemas for Nagare's scheduling domain.

Nagare models a user's tasks, time constraints, schedule blocks, conflicts,
and the decisions made while building and validating a realistic daily flow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

class TimeWindow(BaseModel):
    """A continuous period of time."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_order(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


# ---------------------------------------------------------------------------
# User scheduling preferences
# ---------------------------------------------------------------------------

class CircadianProfile(BaseModel):
    """User's typical energy levels throughout the day.

    Energy values are on a 0–5 scale.
    """

    morning_energy: int = Field(ge=0, le=5)
    afternoon_energy: int = Field(ge=0, le=5)
    evening_energy: int = Field(ge=0, le=5)

    peak_periods: list[TimeWindow] = Field(default_factory=list)


class UserScheduleProfile(BaseModel):
    """Constraints and preferences used when building a schedule."""

    available_windows: list[TimeWindow] = Field(default_factory=list)

    circadian_profile: CircadianProfile

    preferred_work_periods: list[TimeWindow] = Field(default_factory=list)

    protected_blocks: list[TimeWindow] = Field(default_factory=list)

    preferred_session_length: int = Field(gt=0)
    preferred_break_length: int = Field(gt=0)

    sleep_window: TimeWindow

    commute_windows: list[TimeWindow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

class ScheduleBlock(BaseModel):
    """A concrete block placed on the user's schedule.

    A block may represent a task, break, protected time, or commute.
    """

    id: str

    task_id: str | None = None

    start: datetime
    end: datetime

    block_type: Literal[
        "task",
        "break",
        "protected",
        "commute",
    ]

    locked: bool = False

    @model_validator(mode="after")
    def validate_order(self) -> ScheduleBlock:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class Task(BaseModel):
    """A task the user wants Nagare to schedule."""

    id: str

    title: str
    description: str | None = None

    estimated_duration: int = Field(gt=0)

    earliest_start: datetime | None = None
    latest_finish: datetime | None = None

    deadline: datetime | None = None
    deadline_type: Literal["none", "hard", "soft"] = "none"

    # Scheduling flexibility
    fixed: bool = False
    movable: bool = True
    protected: bool = False

    fragmentable: bool = False
    min_fragment_duration: int | None = None
    preferred_fragment_duration: int | None = None

    # Importance
    priority: int = Field(default=3, ge=1, le=5)
    consequence_of_delay: int = Field(default=3, ge=1, le=5)

    # Cognitive requirements
    energy_required: int = Field(default=3, ge=0, le=5)
    cognitive_load: int = Field(default=3, ge=0, le=5)

    # Dependencies
    dependencies: list[str] = Field(default_factory=list)

    # Context
    category: str | None = None
    location: str | None = None
    required_resources: list[str] = Field(default_factory=list)

    # Session constraints
    max_continuous_duration: int | None = None
    break_required: bool = False

    @model_validator(mode="after")
    def validate_constraints(self) -> Task:
        # A fixed task cannot also be movable.
        if self.fixed and self.movable:
            raise ValueError(
                "A fixed task cannot be movable"
            )

        # Fragmentable tasks need a minimum fragment size.
        if self.fragmentable and self.min_fragment_duration is None:
            raise ValueError(
                "fragmentable tasks require min_fragment_duration"
            )

        # Preferred fragment duration should not be smaller
        # than the minimum allowed fragment duration.
        if (
            self.preferred_fragment_duration is not None
            and self.min_fragment_duration is not None
            and self.preferred_fragment_duration
            < self.min_fragment_duration
        ):
            raise ValueError(
                "preferred_fragment_duration must be "
                "greater than or equal to min_fragment_duration"
            )

        # A task's latest finish cannot be before its earliest start.
        if (
            self.earliest_start is not None
            and self.latest_finish is not None
            and self.latest_finish <= self.earliest_start
        ):
            raise ValueError(
                "latest_finish must be after earliest_start"
            )

        # A hard/soft deadline should be explicitly typed.
        if self.deadline is not None and self.deadline_type == "none":
            raise ValueError("deadline_type must be hard or soft when deadline is provided")

        # A typed deadline without a deadline has no meaning.
        if self.deadline is None and self.deadline_type != "none":
            raise ValueError(
                "deadline_type requires a deadline"
            )

        # Maximum continuous duration must be positive.
        if (
            self.max_continuous_duration is not None
            and self.max_continuous_duration <= 0
        ):
            raise ValueError(
                "max_continuous_duration must be greater than 0"
            )

        return self


# ---------------------------------------------------------------------------
# Validation / conflicts
# ---------------------------------------------------------------------------

class Conflict(BaseModel):
    """A concrete reason why a proposed schedule is invalid."""

    task_id: str | None = None

    conflicting_block_id: str | None = None

    conflict_type: Literal[
        "time_overlap",
        "deadline",
        "protected_block",
        "sleep",
        "commute",
        "availability",
        "resource",
        "location",
        "duration",
        "dependency",
        "earliest_start",
        "latest_finish",
        "fragmentation",
        "break",
        "locked_block",
        "unresolved_constraint",
    ]

    severity: int = Field(ge=0, le=5)

    resolvable: bool = True

    explanation: str


class RescheduleAttempt(BaseModel):
    """A record of one attempt to repair an invalid schedule."""

    attempt_number: int = Field(gt=0)

    reason: str

    changes: list[str] = Field(default_factory=list)

    conflicts_found: list[Conflict] = Field(default_factory=list)

    outcome: Literal[
        "resolved",
        "partially_resolved",
        "unresolved",
    ]


# ---------------------------------------------------------------------------
# Proposed schedule
# ---------------------------------------------------------------------------

class ProposedSchedule(BaseModel):
    """A schedule proposed by Nagare's planning step."""

    blocks: list[ScheduleBlock] = Field(default_factory=list)

    reasoning: str | None = None


# ---------------------------------------------------------------------------
# Schedule validation result
# ---------------------------------------------------------------------------

class ScheduleValidationResult(BaseModel):
    """Deterministic result of checking a proposed schedule."""

    status: Literal["PASS", "BLOCK"]

    conflicts: list[Conflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_consistency(self) -> ScheduleValidationResult:
        if self.status == "PASS" and self.conflicts:
            raise ValueError(
                "A PASS result cannot contain conflicts"
            )

        if self.status == "BLOCK" and not self.conflicts:
            raise ValueError(
                "A BLOCK result must contain at least one conflict"
            )

        return self


# ---------------------------------------------------------------------------
# Final scheduling decision
# ---------------------------------------------------------------------------

class SchedulingDecision(BaseModel):
    """Final outcome of Nagare's scheduling workflow."""

    status: Literal[
        "scheduled",
        "needs_user_input",
        "failed",
    ]

    schedule: ProposedSchedule | None = None

    validation: ScheduleValidationResult | None = None

    reschedule_attempts: list[RescheduleAttempt] = Field(
        default_factory=list
    )

    explanation: str | None = None

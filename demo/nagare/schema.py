"""
Nagare domain + Agentic Slice Kit schemas.

The scheduling models describe the actual scheduling problem.
The Agentic Slice Kit models describe the opportunity-validation gate.

Nothing crosses an agent step boundary as unstructured prose.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ============================================================
# NAGARE — SCHEDULING DOMAIN MODELS
# ============================================================


class TimeWindow(BaseModel):
    """A continuous period of time."""

    start: datetime
    end: datetime


class CircadianProfile(BaseModel):
    """User's typical energy levels throughout the day."""

    morning_energy: int = Field(
        ge=0,
        le=5,
        description="Typical morning energy level from 0 to 5",
    )

    afternoon_energy: int = Field(
        ge=0,
        le=5,
        description="Typical afternoon energy level from 0 to 5",
    )

    evening_energy: int = Field(
        ge=0,
        le=5,
        description="Typical evening energy level from 0 to 5",
    )

    peak_periods: list[TimeWindow] = Field(
        default_factory=list,
        description="Periods when the user typically has peak energy/focus",
    )


class ScheduleBlock(BaseModel):
    """A concrete block placed on the user's schedule."""

    task_id: str
    start: datetime
    end: datetime

    block_type: Literal[
        "task",
        "break",
        "protected",
        "commute",
    ]

    locked: bool = False


class UserScheduleProfile(BaseModel):
    """Everything the scheduler needs to understand the user's schedule."""

    # Availability
    available_windows: list[TimeWindow] = Field(
        default_factory=list,
        description="Periods during which the user is available",
    )

    # Circadian / energy
    circadian_profile: CircadianProfile
    preferred_work_periods: list[TimeWindow] = Field(
        default_factory=list,
        description="Periods preferred for focused work",
    )

    # Protected routines
    protected_blocks: list[ScheduleBlock] = Field(
        default_factory=list,
        description="Blocks that should not normally be moved",
    )

    # Scheduling preferences
    preferred_session_length: int = Field(
        gt=0,
        description="Preferred work-session length in minutes",
    )

    preferred_break_length: int = Field(
        gt=0,
        description="Preferred break length in minutes",
    )

    # Personal constraints
    sleep_window: TimeWindow
    commute_windows: list[TimeWindow] = Field(
        default_factory=list,
        description="Periods occupied by commuting",
    )


class Task(BaseModel):
    """A task that the scheduler needs to place."""

    # Identity
    id: str
    title: str
    description: str | None = None

    # Time
    estimated_duration: int = Field(
        gt=0,
        description="Estimated total duration in minutes",
    )

    earliest_start: datetime | None = None
    latest_finish: datetime | None = None

    # Deadline
    deadline: datetime | None = None

    deadline_type: Literal[
        "none",
        "soft",
        "hard",
    ] = "none"

    # Flexibility
    fixed: bool = False
    movable: bool = True
    fragmentable: bool = False

    min_fragment_duration: int | None = Field(
        default=None,
        gt=0,
        description="Minimum duration of an individual fragment",
    )

    preferred_fragment_duration: int | None = Field(
        default=None,
        gt=0,
        description="Preferred duration of an individual fragment",
    )

    # Importance
    priority: int = Field(
        ge=0,
        le=5,
        description="Task priority from 0 to 5",
    )

    consequence_of_delay: int = Field(
        ge=0,
        le=5,
        description="Consequence of delaying this task from 0 to 5",
    )

    # Context
    category: str
    location: str | None = None

    required_resources: list[str] = Field(
        default_factory=list,
    )

    # Constraints
    protected: bool = False

    max_continuous_duration: int | None = Field(
        default=None,
        gt=0,
        description="Maximum amount of continuous work in minutes",
    )

    break_required: bool = False


class Conflict(BaseModel):
    """A detected conflict between a task and an existing schedule block."""

    task_id: str
    conflicting_block_id: str

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
    ]

    severity: int = Field(
        ge=0,
        le=5,
        description="Conflict severity from 0 to 5",
    )

    resolvable: bool

    explanation: str


class RescheduleAttempt(BaseModel):
    """One attempt by the scheduler to resolve a conflict."""

    attempt_number: int = Field(
        ge=1,
    )

    reason: str

    changes: list[str] = Field(
        default_factory=list,
        description="Concrete changes made during this attempt",
    )

    conflicts_found: list[str] = Field(
        default_factory=list,
        description="IDs or descriptions of conflicts discovered",
    )

    outcome: Literal[
        "revised",
        "ask_user",
        "accepted",
        "failed",
    ]


# ============================================================
# OPTIONAL: FINAL SCHEDULE CONTAINER
# ============================================================


class Schedule(BaseModel):
    """Complete schedule produced by the scheduling agent."""

    blocks: list[ScheduleBlock] = Field(
        default_factory=list,
    )

    conflicts: list[Conflict] = Field(
        default_factory=list,
    )

    reschedule_attempts: list[RescheduleAttempt] = Field(
        default_factory=list,
    )


# ============================================================
# AGENTIC SLICE KIT
# ============================================================


class OpportunityRecord(BaseModel):
    """
    What SPOT produces.

    NOTE what is absent:
    no solution, no value proposition, no pitch.
    None of them has been earned yet.
    """

    problem: str = Field(
        description=(
            "What is bad today. "
            "Stated so that it could be shown to be false."
        )
    )

    who_specifically: str = Field(
        description=(
            "A person in a situation. "
            "Never a category."
        )
    )

    current_alternative: str = Field(
        description=(
            "What they actually do right now instead."
        )
    )

    why_now: str = Field(
        description=(
            "What CHANGED. "
            "A trend is not a change."
        )
    )


class Objection(BaseModel):
    """
    One defect in THIS thesis.

    Never generic advice.
    """

    field: str = Field(
        description=(
            "Which field of OpportunityRecord is at fault."
        )
    )

    problem: str = Field(
        description=(
            "The specific defect, quoting the offending text."
        )
    )


class Verdict(BaseModel):
    """
    What the gate produces.

    The only record here that moves the run.
    """

    status: Literal[
        "PASS",
        "BLOCK",
    ]

    objections: list[Objection] = Field(
        default_factory=list,
    )


# ============================================================
# EXAMPLE USAGE
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Example Time Window
    # --------------------------------------------------------

    morning = TimeWindow(
        start=datetime(2026, 9, 19, 9, 0),
        end=datetime(2026, 9, 19, 13, 0),
    )

    sleep = TimeWindow(
        start=datetime(2026, 9, 19, 23, 0),
        end=datetime(2026, 9, 20, 7, 0),
    )

    # --------------------------------------------------------
    # Example Circadian Profile
    # --------------------------------------------------------

    circadian = CircadianProfile(
        morning_energy=5,
        afternoon_energy=3,
        evening_energy=4,
        peak_periods=[
            TimeWindow(
                start=datetime(2026, 9, 19, 9, 0),
                end=datetime(2026, 9, 19, 11, 0),
            )
        ],
    )

    # --------------------------------------------------------
    # Example User Profile
    # --------------------------------------------------------

    user_profile = UserScheduleProfile(
        available_windows=[morning],
        circadian_profile=circadian,
        preferred_work_periods=[
            TimeWindow(
                start=datetime(2026, 9, 19, 9, 0),
                end=datetime(2026, 9, 19, 12, 0),
            )
        ],
        protected_blocks=[],
        preferred_session_length=50,
        preferred_break_length=10,
        sleep_window=sleep,
        commute_windows=[],
    )

    # --------------------------------------------------------
    # Example Task
    # --------------------------------------------------------

    task = Task(
        id="task-001",
        title="Prepare Agent-a-Thon submission",
        description="Finish the agent specification and test cases.",
        estimated_duration=120,
        earliest_start=datetime(2026, 9, 19, 9, 0),
        latest_finish=datetime(2026, 9, 19, 18, 0),
        deadline=datetime(2026, 9, 19, 18, 0),
        deadline_type="hard",
        fixed=False,
        movable=True,
        fragmentable=True,
        min_fragment_duration=30,
        preferred_fragment_duration=50,
        priority=5,
        consequence_of_delay=5,
        category="project",
        location="home",
        required_resources=["laptop", "internet"],
        protected=False,
        max_continuous_duration=50,
        break_required=True,
    )

    # --------------------------------------------------------
    # Example Scheduled Block
    # --------------------------------------------------------

    block = ScheduleBlock(
        task_id=task.id,
        start=datetime(2026, 9, 19, 9, 0),
        end=datetime(2026, 9, 19, 9, 50),
        block_type="task",
        locked=False,
    )

    # --------------------------------------------------------
    # Example Conflict
    # --------------------------------------------------------

    conflict = Conflict(
        task_id=task.id,
        conflicting_block_id="protected-001",
        conflict_type="protected_block",
        severity=4,
        resolvable=True,
        explanation=(
            "The proposed task overlaps with a protected routine."
        ),
    )

    # --------------------------------------------------------
    # Example Reschedule Attempt
    # --------------------------------------------------------

    attempt = RescheduleAttempt(
        attempt_number=1,
        reason="Resolve overlap with protected block.",
        changes=[
            "Moved task from 09:00 to 10:00.",
            "Preserved the protected routine.",
        ],
        conflicts_found=[
            "protected-001",
        ],
        outcome="revised",
    )

    # --------------------------------------------------------
    # Example Final Schedule
    # --------------------------------------------------------

    schedule = Schedule(
        blocks=[block],
        conflicts=[conflict],
        reschedule_attempts=[attempt],
    )

    # --------------------------------------------------------
    # Example Opportunity Record
    # --------------------------------------------------------

    opportunity = OpportunityRecord(
        problem=(
            "Students repeatedly lose track of tasks when their "
            "available time changes during the day."
        ),
        who_specifically=(
            "A college student whose class schedule changes "
            "throughout the week."
        ),
        current_alternative=(
            "They manually move tasks between a calendar and "
            "a to-do list."
        ),
        why_now=(
            "Their schedule recently became more variable because "
            "of changing class and project commitments."
        ),
    )

    # --------------------------------------------------------
    # Example Verdict
    # --------------------------------------------------------

    verdict = Verdict(
        status="PASS",
        objections=[],
    )

    # Print validated models
    print("USER PROFILE")
    print(user_profile.model_dump_json(indent=2))

    print("\nTASK")
    print(task.model_dump_json(indent=2))

    print("\nSCHEDULE")
    print(schedule.model_dump_json(indent=2))

    print("\nOPPORTUNITY")
    print(opportunity.model_dump_json(indent=2))

    print("\nVERDICT")
    print(verdict.model_dump_json(indent=2))
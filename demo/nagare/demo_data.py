from datetime import datetime

from .schema import (
    CircadianProfile,
    Task,
    TimeWindow,
    UserScheduleProfile,
)


def demo_user_profile() -> UserScheduleProfile:
    return UserScheduleProfile(
        available_windows=[
            TimeWindow(
                start=datetime(2026, 9, 19, 9, 0),
                end=datetime(2026, 9, 19, 21, 0),
            )
        ],
        circadian_profile=CircadianProfile(
            morning_energy=5,
            afternoon_energy=3,
            evening_energy=4,
            peak_periods=[
                TimeWindow(
                    start=datetime(2026, 9, 19, 9, 0),
                    end=datetime(2026, 9, 19, 11, 0),
                )
            ],
        ),
        preferred_work_periods=[
            TimeWindow(
                start=datetime(2026, 9, 19, 9, 0),
                end=datetime(2026, 9, 19, 12, 0),
            )
        ],
        protected_blocks=[],
        preferred_session_length=50,
        preferred_break_length=10,
        sleep_window=TimeWindow(
            start=datetime(2026, 9, 19, 23, 0),
            end=datetime(2026, 9, 20, 7, 0),
        ),
        commute_windows=[],
    )


def demo_tasks() -> list[Task]:
    return [
        Task(
            id="task-001",
            title="Finish Agent-a-Thon submission",
            description="Finish the specification and testing.",
            estimated_duration=120,
            earliest_start=datetime(2026, 9, 19, 9, 0),
            latest_finish=datetime(2026, 9, 19, 18, 0),
            deadline=datetime(2026, 9, 19, 18, 0),
            deadline_type="hard",
            priority=5,
            consequence_of_delay=5,
            energy_required=5,
            cognitive_load=4,
            category="project",
            required_resources=["laptop", "internet"],
            fragmentable=True,
            min_fragment_duration=30,
            preferred_fragment_duration=50,
            max_continuous_duration=50,
            break_required=True,
        ),
        Task(
            id="task-002",
            title="Study Computer Networks",
            estimated_duration=90,
            priority=3,
            consequence_of_delay=2,
            energy_required=4,
            cognitive_load=4,
            category="study",
            fragmentable=True,
            min_fragment_duration=30,
            preferred_fragment_duration=45,
        ),
    ]

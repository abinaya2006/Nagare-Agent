#!/usr/bin/env python3
"""
Nagare Terminal Interface — Interactive CLI for the Nagare Scheduling Agent.

This module provides a full terminal-based interaction environment between the user
and the Nagare Agent (NANI). It allows:
- Generating and visualizing baseline schedules aligned with circadian energy levels.
- Simulating interruptions and missed tasks with automatic agent rescheduling.
- Real-time Human-in-the-Loop callbacks: when the agent hits a conflict it cannot resolve
  alone, it suspends execution and prompts the user directly in the terminal to decide.
- Adding, editing, and inspecting daily tasks and profile constraints.
- Direct conversational Q&A with the agent regarding workload and energy alignment.
- Replaying and auditing past runs from the immutable append-only state store.

Usage:
    python demo/terminal.py                  # Start interactive terminal session
    python demo/terminal.py --run            # Run baseline schedule non-interactively
    python demo/terminal.py --miss task-001  # Reschedule a missed task
    python demo/terminal.py --smoke          # Run smoke test thesis evaluator agent
    python demo/terminal.py --pending        # Inspect pending human-in-the-loop questions
    python demo/terminal.py --replay <id>    # Replay all steps of a run
"""
from __future__ import annotations
from demo.nagare.validator import validate_schedule
from demo.nagare.schema import (
    CircadianProfile,
    ProposedSchedule,
    ScheduleBlock,
    Task,
    TimeWindow,
    UserScheduleProfile,
)
from demo.nagare.planner import baseline_schedule, reschedule_task
from demo.nagare.flow import build_flow as build_nagare_flow
from demo.nagare.demo_data import demo_tasks, demo_user_profile
from slice.store import Store
from slice.records import RunState
from slice.config import settings as load_settings, Settings
from slice import callback, runner

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import textwrap
from typing import Any

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Terminal Styling Helpers
# ---------------------------------------------------------------------------

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
AMBER = "\033[33m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"


def _c(text: str, color: str) -> str:
    """Format text with ANSI escape codes if output is a TTY or explicitly supported."""
    return f"{color}{text}{RESET}"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


# ---------------------------------------------------------------------------
# Visual Formatters
# ---------------------------------------------------------------------------

def print_banner(db_path: str, st: Settings) -> None:
    print(_c("=" * 68, CYAN))
    print(
        f"{_c('🌊 NAGARE AGENT — INTERACTIVE TERMINAL', BOLD + CYAN)}"
        f"  {_c('[NANI v1.0]', DIM)}"
    )
    print(_c("Energy-Aware Scheduling & Autonomous Rescheduling Flow", DIM))
    print(_c("=" * 68, CYAN))
    mode_str = "Live API" if st.api_key else "Local Rule-Based / Offline"
    print(
        f"{_c('Database:', DIM)} {db_path}   "
        f"{_c('Model Mode:', DIM)} {_c(mode_str, GREEN if st.api_key else AMBER)}"
    )
    print(_c("-" * 68, DIM))


def format_energy_bar(energy: int) -> str:
    filled = "⚡" * energy
    empty = "·" * (5 - energy)
    return f"{filled}{_c(empty, DIM)}"


def print_tasks_table(tasks: list[Task]) -> None:
    print(f"\n{_c('📋 Current Tasks:', BOLD)}")
    print(
        f"{_c('ID', DIM):<12} {_c('Title', DIM):<30} {_c('Duration', DIM):<10} "
        f"{_c('Priority', DIM):<10} {_c('Energy', DIM):<10} {_c('Deadline', DIM):<18}"
    )
    print(_c("-" * 92, DIM))
    for t in tasks:
        dl_str = t.deadline.strftime("%H:%M") if t.deadline else "None"
        if t.deadline_type != "none":
            dl_str += f" ({t.deadline_type})"
        prio_str = f"P{t.priority}" + (" [Fixed]" if t.fixed else "")
        energy_str = f"{t.energy_required}/5"
        print(
            f"{_c(t.id, CYAN):<21} {t.title[:28]:<30} {f'{t.estimated_duration}m':<10} "
            f"{prio_str:<10} {energy_str:<10} {dl_str:<18}"
        )
    print()


def print_schedule_table(
    schedule: ProposedSchedule,
    tasks_by_id: dict[str, Task],
    title: str = "Proposed Schedule",
) -> None:
    print(f"\n{_c('📅 ' + title + ':', BOLD)}")
    if not schedule.blocks:
        print(_c("  (No scheduled blocks found)", AMBER))
        return

    print(
        f"  {_c('Time Window', DIM):<18} {_c('Duration', DIM):<10} "
        f"{_c('Type', DIM):<12} {_c('Block / Task Details', DIM):<35} {_c('Status', DIM)}"
    )
    print("  " + _c("-" * 88, DIM))

    for b in schedule.blocks:
        time_str = f"{b.start.strftime('%H:%M')} – {b.end.strftime('%H:%M')}"
        dur_mins = int((b.end - b.start).total_seconds() // 60)
        dur_str = f"{dur_mins}m"

        task_info = b.task_id or b.id
        if b.task_id and b.task_id in tasks_by_id:
            t = tasks_by_id[b.task_id]
            task_info = f"{t.title} [P{t.priority}, E{t.energy_required}]"

        status_tag = _c("🔒 LOCKED", AMBER) if b.locked else _c(
            "✓ Scheduled", GREEN)
        if b.block_type == "protected":
            status_tag = _c("🛡️ PROTECTED", MAGENTA)
        elif b.block_type == "break":
            status_tag = _c("☕ Break", BLUE)

        type_str = b.block_type.upper()
        print(
            f"  {time_str:<18} {dur_str:<10} {_c(type_str, DIM):<20} "
            f"{task_info[:33]:<35} {status_tag}"
        )
    print()


# ---------------------------------------------------------------------------
# Flow Execution & Terminal Human-in-the-Loop Handler
# ---------------------------------------------------------------------------

def run_agent_flow(
    store: Store,
    tasks: list[Task],
    profile: UserScheduleProfile,
    existing_blocks: list[ScheduleBlock] | None = None,
    missed_task_id: str | None = None,
    interruption_reason: str | None = None,
    interactive_callback: bool = True,
    st: Settings | None = None,
) -> tuple[str, RunState]:
    """Execute the Nagare Agent flow with interactive terminal callback support."""
    st = st or load_settings()
    run_id = store.create_run("nagare")

    existing_blocks = existing_blocks or []
    input_payload: dict[str, Any] = {
        "profile": profile.model_dump(mode="json"),
        "tasks": [t.model_dump(mode="json") for t in tasks],
        "existing_blocks": [b.model_dump(mode="json") for b in existing_blocks],
    }
    if missed_task_id:
        input_payload["missed_task_id"] = missed_task_id
        input_payload["reason"] = (
            interruption_reason or f"Task '{missed_task_id}' was interrupted or missed."
        )

    store.append(run_id, "input", input_payload, produced_by="system")

    print(
        f"\n{_c('▶ Starting Nagare Agent Run:', BOLD + CYAN)} "
        f"{_c(run_id, BOLD)} {_c('[State: DRAFTING]', DIM)}"
    )
    if missed_task_id:
        print(
            f"  {_c('Interruption Trigger:', AMBER)} "
            f"Missed task {_c(missed_task_id, BOLD)} — {input_payload.get('reason')}"
        )

    model_call = None
    if st.api_key:
        from slice.llm import complete
        model_call = complete
    flow = build_nagare_flow(model_call)
    state = runner.advance(store, run_id, flow, st)

    # If the state machine pauses for a human decision, handle it interactively
    while state is RunState.AWAITING_EXPERT:
        print(
            f"\n{_c('⏸️  HUMAN DECISION REQUIRED (AWAITING_EXPERT)', BOLD + AMBER)}")
        print(
            _c(
                "The agent encountered a constraint conflict that cannot be safely "
                "resolved without user authorization.",
                DIM,
            )
        )

        pending_qs = callback.pending(store, run_id)
        if not pending_qs:
            print(_c("No pending question found in store. Resuming...", DIM))
            break

        q = pending_qs[0]
        print(f"\n{_c('Question from Agent:', BOLD)} {q.question}")
        if q.context and "conflicts" in q.context:
            print(f"{_c('Conflict Details:', DIM)}")
            for c in q.context["conflicts"]:
                print(
                    f"  • {_c(c.get('conflict_type', 'conflict'), RED)}: {c.get('explanation', '')}")

        if not interactive_callback:
            print(_c("Non-interactive mode: Run is suspended in database.", AMBER))
            return run_id, state

        print("\nChoose how you want to resolve this:")
        print("  [1] Shorten the missed task duration")
        print("  [2] Move the blocking task to tomorrow")
        print("  [3] Move the missed task to tomorrow")
        print("  [4] Decline / Leave task unresolved")
        print("  [5] Custom text instruction")

        ans_choice = input(
            f"{_c('Your Decision [1-5 or custom text]> ', BOLD)}").strip()
        answer_text = ""
        if ans_choice == "1":
            ans_dur = input(
                "Enter new shortened duration in minutes (e.g. 45): ").strip() or "45"
            answer_text = f"shorten the missed task to {ans_dur} minutes"
        elif ans_choice == "2":
            answer_text = "move the blocking task to tomorrow"
        elif ans_choice == "3":
            answer_text = "move the missed task to tomorrow"
        elif ans_choice == "4":
            answer_text = "leave it missed and do not move other tasks"
        elif ans_choice == "5":
            answer_text = input(
                "Enter your custom scheduling instruction: ").strip()
        else:
            answer_text = ans_choice

        if not answer_text:
            answer_text = "shorten the missed task"

        print(f"  {_c('✓ Recording answer:', GREEN)} \"{answer_text}\"")
        callback.answer(store, q.id, answer_text, who="terminal_user")
        print(f"  {_c('▶ Resuming Nagare Agent flow...', CYAN)}")
        state = runner.advance(store, run_id, flow, st)

    # Report results
    tasks_by_id = {t.id: t for t in tasks}
    print(f"\n{_c('🏁 Run Finished:', BOLD)} State = ", end="")
    if state is RunState.COMPLETE:
        print(_c("COMPLETE (PASS)", BOLD + GREEN))
    elif state is RunState.FAILED:
        print(_c("FAILED", BOLD + RED))
    else:
        print(_c(state.value.upper(), AMBER))

    # Print trace summary
    print(f"\n{_c('📜 Execution Trace:', BOLD)}")
    for v in store.replay(run_id):
        k = v.kind
        by = v.produced_by
        if k == "proposed_schedule":
            num_blocks = len(v.payload.get("blocks", []))
            print(f"  {_c(f'#{v.seq}', DIM)} {_c('DRAFT', CYAN)}: Proposed {
                  num_blocks} blocks {_c(f'({by})', DIM)}")
        elif k == "validation":
            st_val = v.payload.get("status")
            confs = len(v.payload.get("conflicts", []))
            color = GREEN if st_val == "PASS" else RED
            print(f"  {_c(f'#{v.seq}', DIM)} {_c('GATING', color)}: Validation {
                  st_val} ({confs} conflicts) {_c(f'({by})', DIM)}")
            for cf in v.payload.get("conflicts", []):
                print(f"         {_c('!', RED)} {cf.get('explanation')}")
        elif k == "question":
            print(f"  {_c(f'#{v.seq}', DIM)} {_c('CALLBACK', AMBER)
                                              }: Parked question for user {_c(f'({by})', DIM)}")
        elif k == "expert_answer":
            print(f"  {_c(f'#{v.seq}', DIM)} {_c('USER_ANSWER', GREEN)}: \"{
                  v.payload.get('answer')}\" {_c(f'({by})', DIM)}")
        elif k == "decision":
            print(f"  {_c(f'#{v.seq}', DIM)} {_c('DECISION', GREEN)}: {
                  v.payload.get('explanation', 'Finished')} {_c(f'({by})', DIM)}")
        elif k == "failure":
            print(f"  {_c(f'#{v.seq}', DIM)} {_c('FAILURE', RED)}: {
                  v.payload.get('kind')} — {v.payload.get('detail')} {_c(f'({by})', DIM)}")

    # Display final schedule if available
    latest_sched = store.latest(run_id, "proposed_schedule")
    if latest_sched:
        try:
            ps = ProposedSchedule.model_validate(latest_sched)
            print_schedule_table(ps, tasks_by_id, "Final Resulting Schedule")
        except Exception:
            pass

    return run_id, state


# ---------------------------------------------------------------------------
# Interactive Sub-actions
# ---------------------------------------------------------------------------

def interactive_add_task(tasks: list[Task]) -> None:
    print(f"\n{_c('➕ Add New Task', BOLD + CYAN)}")
    title = input("Task Title: ").strip()
    if not title:
        print(_c("Task creation cancelled (empty title).", AMBER))
        return

    tid = f"task-{len(tasks) + 1:03d}"
    print(f"Generated ID: {tid}")

    dur_str = input("Estimated Duration in minutes [default: 60]: ").strip()
    dur = int(dur_str) if dur_str.isdigit() else 60

    prio_str = input("Priority (1=Lowest, 5=Highest) [default: 3]: ").strip()
    prio = int(prio_str) if prio_str.isdigit(
    ) and 1 <= int(prio_str) <= 5 else 3

    energy_str = input(
        "Energy Required (1=Low, 5=Deep Focus) [default: 3]: ").strip()
    energy = int(energy_str) if energy_str.isdigit(
    ) and 0 <= int(energy_str) <= 5 else 3

    cat = input(
        "Category (study, project, personal, admin) [default: study]: ").strip() or "study"

    dl_input = input(
        "Deadline today (HH:MM format, e.g. 18:00, or Enter for None): ").strip()
    deadline = None
    dl_type = "none"
    if dl_input:
        try:
            now = datetime.now()
            parts = [int(p) for p in dl_input.split(":")]
            deadline = now.replace(
                hour=parts[0], minute=parts[1], second=0, microsecond=0)
            dl_type_in = input(
                "Deadline type (hard/soft) [default: hard]: ").strip().lower()
            dl_type = "soft" if dl_type_in == "soft" else "hard"
        except Exception:
            print(_c("Invalid time format; setting no deadline.", AMBER))
            deadline = None
            dl_type = "none"

    new_task = Task(
        id=tid,
        title=title,
        estimated_duration=dur,
        priority=prio,
        energy_required=energy,
        category=cat,
        deadline=deadline,
        deadline_type=dl_type,
    )
    tasks.append(new_task)
    print(_c(f"✓ Task '{title}' ({tid}) added successfully!", GREEN))


def interactive_miss_task(
    store: Store,
    tasks: list[Task],
    profile: UserScheduleProfile,
    last_schedule: ProposedSchedule | None,
    st: Settings,
) -> None:
    print(f"\n{_c('🚨 Simulate Task Interruption / Delay', BOLD + AMBER)}")
    print("Select the task that was interrupted or missed:")
    for idx, t in enumerate(tasks, 1):
        print(f"  [{idx}] {_c(t.id, CYAN)}: {t.title} ({t.estimated_duration}m)")

    choice = input(
        f"{_c('Select task number [1-' + str(len(tasks)) + ']> ', BOLD)}").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(tasks)):
        print(_c("Invalid task selection.", RED))
        return

    selected_task = tasks[int(choice) - 1]
    reason = input(
        "What caused the interruption? (e.g. Lab ran late, urgent call): ").strip()
    if not reason:
        reason = f"{selected_task.title} was interrupted."

    existing_blocks = last_schedule.blocks if last_schedule else []
    run_agent_flow(
        store,
        tasks,
        profile,
        existing_blocks=existing_blocks,
        missed_task_id=selected_task.id,
        interruption_reason=reason,
        interactive_callback=True,
        st=st,
    )


def interactive_chat_with_agent(
    store: Store,
    tasks: list[Task],
    profile: UserScheduleProfile,
    last_schedule: ProposedSchedule | None,
    st: Settings,
) -> None:
    print(f"\n{_c('💬 Chat with Nagare Agent (NANI)', BOLD + CYAN)}")
    print(_c("Ask questions about your day, circadian energy alignment, or scheduling advice. Type 'exit' to return.", DIM))

    while True:
        try:
            query = input(f"\n{_c('You> ', BOLD + GREEN)}").strip()
            if not query:
                continue
            if query.lower() in ("exit", "quit", "back", "q"):
                break

            # If an API key is present, attempt live completion; otherwise use rich deterministic reasoning
            if st.api_key:
                from slice.llm import complete
                from slice.budget import Budget
                budget = Budget(store, "chat_session", st)
                context_summary = {
                    "tasks": [t.model_dump(mode="json") for t in tasks],
                    "circadian": profile.circadian_profile.model_dump(mode="json"),
                    "schedule": [b.model_dump(mode="json") for b in (last_schedule.blocks if last_schedule else [])],
                }
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are NANI, Nagare's intelligent circadian scheduling agent. "
                            "You help students optimize their daily flow, protect sleep and gym buffers, "
                            "and handle task interruptions. Answer concisely and supportively."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Context:\n{json.dumps(context_summary, default=str)}\n\nUser Question: {query}",
                    },
                ]
                print(_c("NANI is thinking...", DIM))
                ans = complete(settings=st, budget=budget,
                               messages=messages, model=st.model)
                print(f"\n{_c('NANI>', BOLD + CYAN)} {ans}")
            else:
                # Deterministic intelligent helper
                q_low = query.lower()
                print(f"\n{_c('NANI [Offline Mode]>', BOLD + CYAN)} ", end="")
                if "energy" in q_low or "circadian" in q_low:
                    print(
                        f"Your circadian peak is between 09:00 and 11:00 (Energy level: "
                        f"{profile.circadian_profile.morning_energy}/5). High-focus tasks "
                        f"like '{tasks[0].title if tasks else 'your top project'}' should be tackled in this window."
                    )
                elif "deadline" in q_low or "urgent" in q_low:
                    hard_tasks = [
                        t for t in tasks if t.deadline_type == "hard"]
                    if hard_tasks:
                        print(
                            f"You have {len(hard_tasks)} hard deadline task(s): {', '.join(t.title for t in hard_tasks)}.")
                    else:
                        print(
                            "You have no hard deadline tasks today. Focus on priority ranking.")
                elif "reschedule" in q_low or "miss" in q_low:
                    print(
                        "To reschedule a missed block, use option [4] (Simulate Interruption) in the main menu.")
                elif "task" in q_low or "list" in q_low:
                    print(
                        f"You currently have {len(tasks)} tasks queued totaling {sum(t.estimated_duration for t in tasks)} minutes of planned work.")
                else:
                    print(
                        "I am monitoring your schedule. All high-energy tasks are placed before afternoon dips, "
                        "and your sleep window is strictly protected from 23:00 to 07:00."
                    )
        except (KeyboardInterrupt, EOFError):
            break


def interactive_show_profile(profile: UserScheduleProfile) -> None:
    print(f"\n{_c('👤 User Schedule & Energy Profile:', BOLD + CYAN)}")
    cp = profile.circadian_profile
    print(f"  • {_c('Morning Energy (09:00-12:00):', DIM)}   {format_energy_bar(cp.morning_energy)} ({cp.morning_energy}/5)")
    print(f"  • {_c('Afternoon Energy (12:00-17:00):', DIM)} {format_energy_bar(cp.afternoon_energy)} ({cp.afternoon_energy}/5)")
    print(f"  • {_c('Evening Energy (17:00-21:00):', DIM)}   {format_energy_bar(cp.evening_energy)} ({cp.evening_energy}/5)")

    sleep_str = f"{profile.sleep_window.start.strftime('%H:%M')} – {profile.sleep_window.end.strftime('%H:%M')}"
    print(f"  • {_c('Sleep Window (Immovable):', MAGENTA)}    {sleep_str}")

    print(f"  • {_c('Preferred Work Block:', DIM)}     {profile.preferred_session_length}m work / {profile.preferred_break_length}m break")
    if profile.available_windows:
        w_str = ", ".join(
            f"{w.start.strftime('%H:%M')}–{w.end.strftime('%H:%M')}" for w in profile.available_windows)
        print(f"  • {_c('Available Waking Windows:', DIM)} {w_str}")
    print()


def interactive_show_pending_questions(store: Store) -> None:
    print(f"\n{_c('📬 Open Human-in-the-Loop Questions:', BOLD + CYAN)}")
    callback.sweep(store)
    open_qs = callback.pending(store)
    if not open_qs:
        print(_c("  ✓ No questions awaiting human input.", GREEN))
        return

    for idx, q in enumerate(open_qs, 1):
        print(
            f"\n  [{idx}] {_c('Question ID:', DIM)} {q.id}  {_c('Run ID:', DIM)} {q.run_id}")
        print(f"      {_c(q.question, BOLD)}")

    ans_idx = input(
        f"\n{_c('Answer a question? Enter number [1-' + str(len(open_qs)) + ' or Enter to cancel]> ', BOLD)}").strip()
    if ans_idx.isdigit() and 1 <= int(ans_idx) <= len(open_qs):
        q = open_qs[int(ans_idx) - 1]
        ans = input(f"Your answer for {q.id}: ").strip()
        if ans:
            callback.answer(store, q.id, ans, who="terminal_user")
            print(
                _c(f"✓ Recorded answer for run {q.run_id}. Run can now be resumed.", GREEN))


def run_smoke_agent(store: Store, st: Settings) -> None:
    """Run the smoke test thesis evaluator agent to demonstrate multi-step back-edge validation."""
    from demo.smoke.flow import build_flow as build_smoke_flow
    from demo.smoke.stub import Stub

    print(
        f"\n{_c('🔬 Running Smoke Test Agent (Founder Thesis Evaluator)...', BOLD + CYAN)}")
    idea = (
        "AI can help fix campus placements - students struggle to get internships "
        "and it is a real problem. Take my batchmate Karthik: 7.2 CGPA, two Android "
        "apps live on the Play Store, and across the whole of last year's cycle he "
        "was auto-rejected within an hour of applying, every single time, without a "
        "human ever opening his file. He got in eventually through a senior. The "
        "portal only started filtering below 8.0 from the 2025 cycle."
    )

    run_id = store.create_run("smoke")
    store.append(run_id, "input", {"text": idea}, produced_by="system")

    call_impl = Stub() if not st.api_key else None
    if call_impl is None:
        from slice.llm import complete as live_complete
        call_impl = live_complete

    flow = build_smoke_flow(call_impl)
    final = runner.advance(store, run_id, flow, st)

    for v in store.replay(run_id):
        if v.kind == "opportunity":
            print(
                f"  {_c('SPOT', CYAN)}: Drafted thesis opportunity {_c(f'(seq #{v.seq})', DIM)}")
        elif v.kind == "verdict":
            tag = _c("BLOCK", AMBER) if v.payload.get(
                "status") == "BLOCK" else _c("PASS", GREEN)
            print(
                f"  {_c('GATE', BOLD)}: {tag} ({len(v.payload.get('objections', []))} objections)")
            for o in v.payload.get("objections", []):
                print(
                    f"        {_c('-', DIM)} {o.get('field')}: {o.get('problem')[:70]}...")

    print(f"\n  {_c('=> Outcome:', BOLD)} {_c(final.value.upper(), GREEN if final is RunState.COMPLETE else RED)}")


def replay_run(store: Store, run_id: str) -> None:
    print(f"\n{_c('📼 Replay Log for Run:', BOLD + CYAN)} {_c(run_id, BOLD)}")
    events = store.replay(run_id)
    if not events:
        print(_c("No events found for this run ID.", RED))
        return

    for v in events:
        body = json.dumps(v.payload, indent=2, default=str)
        print(f"\n{_c(f'#{v.seq}', DIM)} {_c(v.kind, BOLD)} {
              _c(f'by {v.produced_by}', CYAN)}")
        for line in body.splitlines()[:15]:
            print(f"    {line}")
        if len(body.splitlines()) > 15:
            print(f"    {_c('... (truncated)', DIM)}")
    print()


# ---------------------------------------------------------------------------
# Main Interactive Loop
# ---------------------------------------------------------------------------

def main_interactive_menu(db_path: str) -> None:
    store = Store(db_path)
    st = load_settings()
    tasks = demo_tasks()
    profile = demo_user_profile()
    last_schedule: ProposedSchedule | None = None

    print_banner(db_path, st)

    while True:
        print(f"\n{_c('Main Menu:', BOLD)}")
        print(
            f"  {_c('[1]', CYAN)} Plan / Run Baseline Schedule (Agent Workflow)")
        print(f"  {_c('[2]', CYAN)} View Current Tasks ({len(tasks)} loaded)")
        print(f"  {_c('[3]', CYAN)} Add New Task")
        print(
            f"  {_c('[4]', CYAN)} Simulate Interruption / Missed Task (Auto-Reschedule)")
        print(f"  {_c('[5]', CYAN)} Chat with Nagare Agent (NANI)")
        print(f"  {_c('[6]', CYAN)} Check Pending Human-in-the-Loop Questions")
        print(
            f"  {_c('[7]', CYAN)} View User Circadian Profile & Energy Windows")
        print(f"  {_c('[8]', CYAN)} Run Smoke Test Agent (Spot & Gate)")
        print(f"  {_c('[9]', CYAN)} Replay Run History")
        print(f"  {_c('[0]', RED)}  Exit")

        try:
            choice = input(f"\n{_c('nagare> ', BOLD + GREEN)}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting Nagare Terminal.")
            break

        if choice in ("1", "plan", "schedule", "run"):
            run_id, state = run_agent_flow(
                store,
                tasks,
                profile,
                existing_blocks=[],
                interactive_callback=True,
                st=st,
            )
            latest_sched = store.latest(run_id, "proposed_schedule")
            if latest_sched:
                last_schedule = ProposedSchedule.model_validate(latest_sched)

        elif choice in ("2", "tasks", "list"):
            print_tasks_table(tasks)

        elif choice in ("3", "add"):
            interactive_add_task(tasks)

        elif choice in ("4", "miss", "reschedule", "interrupt"):
            interactive_miss_task(store, tasks, profile, last_schedule, st)

        elif choice in ("5", "chat", "ask"):
            interactive_chat_with_agent(
                store, tasks, profile, last_schedule, st)

        elif choice in ("6", "pending", "questions"):
            interactive_show_pending_questions(store)

        elif choice in ("7", "profile", "energy"):
            interactive_show_profile(profile)

        elif choice in ("8", "smoke"):
            run_smoke_agent(store, st)

        elif choice in ("9", "replay", "history"):
            rid = input("Enter Run ID to replay: ").strip()
            if rid:
                replay_run(store, rid)

        elif choice in ("0", "exit", "quit", "q"):
            print(_c("Goodbye! Keep your day in flow.", CYAN))
            break

        elif choice in ("cls", "clear"):
            clear_screen()
            print_banner(db_path, st)

        elif choice in ("help", "?"):
            print(f"\n{_c('Commands Cheat Sheet:', BOLD)}")
            print("  1 / plan      - Run full baseline planning agent")
            print("  2 / tasks     - List all tasks")
            print("  3 / add       - Add new task")
            print("  4 / miss      - Simulate missed block and auto-reschedule")
            print("  5 / chat      - Ask NANI scheduling & energy questions")
            print("  6 / pending   - Answer pending human-in-the-loop callbacks")
            print("  7 / profile   - Inspect circadian energy profile")
            print("  8 / smoke     - Run thesis evaluator smoke agent")
            print("  9 / replay    - Replay a specific run's audit trail")
            print("  clear         - Clear terminal screen")
            print("  0 / exit      - Quit terminal")

        else:
            print(_c("Unknown option. Type 'help' for command list.", AMBER))


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nagare Terminal Interface — Interactive CLI for the Nagare Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="run.db",
                        help="SQLite database path (default: run.db)")
    parser.add_argument("--run", action="store_true",
                        help="Run baseline schedule non-interactively")
    parser.add_argument("--miss", metavar="TASK_ID",
                        help="Trigger reschedule for a specific task ID")
    parser.add_argument("--smoke", action="store_true",
                        help="Run the smoke test evaluator agent")
    parser.add_argument("--pending", action="store_true",
                        help="List all open human callback questions")
    parser.add_argument("--replay", metavar="RUN_ID",
                        help="Replay events for a specific run ID")

    args = parser.parse_args()
    store = Store(args.db)
    st = load_settings()
    tasks = demo_tasks()
    profile = demo_user_profile()

    if args.run:
        print_banner(args.db, st)
        run_agent_flow(store, tasks, profile, interactive_callback=True, st=st)
        return 0

    if args.miss:
        print_banner(args.db, st)
        run_agent_flow(
            store,
            tasks,
            profile,
            missed_task_id=args.miss,
            interruption_reason=f"Task {args.miss} was interrupted.",
            interactive_callback=True,
            st=st,
        )
        return 0

    if args.smoke:
        print_banner(args.db, st)
        run_smoke_agent(store, st)
        return 0

    if args.pending:
        interactive_show_pending_questions(store)
        return 0

    if args.replay:
        replay_run(store, args.replay)
        return 0

    # Start full interactive REPL menu
    main_interactive_menu(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

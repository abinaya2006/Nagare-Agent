"""
The page a user answers on when NANI can't resolve a schedule conflict alone.

This is the human-in-the-loop half of Nagare's back-edge. The agent suspends a
run (AWAITING_EXPERT) and parks a question when it hits a conflict it cannot
safely resolve on its own - e.g. a hard-deadline task that has no available
window without violating sleep or a protected block. The user opens this page,
answers, and the run resumes with their answer recorded as a distinct class of
evidence: not something NANI inferred, something the user actually said.

Unlike the smoke demo's original framing, the person answering here isn't a
third-party domain expert - it's the same user whose schedule it is. Same
callback mechanism, different actor, so the copy below speaks to "you" and to
schedule conflicts specifically rather than startup theses.

Server-rendered, no JavaScript, no build step. The user may be on a phone
between tasks, and this has to work there.

Run it, then expose it:

    uvicorn web.expert:app --host 0.0.0.0 --port 8000
    cloudflared tunnel --url http://localhost:8000

The tunnel needs no account and prints a public URL. For the hackathon demo
that URL can be sent to whoever's schedule is being tested; in a real
deployment this page would just be a view inside Nagare's own app instead of a
separate tunneled link.
"""
from __future__ import annotations

import html
import os

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import FastAPI

from slice import callback
from slice.config import settings
from slice.store import Store

DB = os.environ.get("SLICE_DB", "run.db")
app = FastAPI(title="Nagare — schedule check-in")


def _store() -> Store:
    return Store(DB)


PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{color-scheme:light dark}}
body{{font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;max-width:38rem;
margin:0 auto;padding:2rem 1.2rem 4rem}}
h1{{font-size:1.35rem;margin:0 0 .3rem}}
.sub{{color:#6b7280;font-size:.9rem;margin:0 0 1.8rem}}
.card{{border:1px solid #d4d4d8;border-radius:8px;padding:1.1rem 1.2rem;margin:0 0 1rem}}
.q{{font-size:1.1rem;font-weight:600;margin:0 0 .8rem}}
.ctx{{background:rgba(127,127,127,.09);border-radius:6px;padding:.8rem 1rem;
font-size:.9rem;margin:0 0 1.2rem;white-space:pre-wrap;overflow-wrap:anywhere}}
.ctx b{{display:block;font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
color:#6b7280;margin-bottom:.35rem;font-weight:600}}
textarea{{width:100%;min-height:9rem;font:inherit;padding:.7rem;border:1px solid #a1a1aa;
border-radius:6px;background:transparent;color:inherit}}
button{{font:inherit;font-weight:600;padding:.6rem 1.4rem;margin-top:.8rem;
border:0;border-radius:6px;background:#0d5c5f;color:#fff;cursor:pointer}}
a{{color:#0d5c5f}} .empty{{color:#6b7280}}
.note{{font-size:.85rem;color:#6b7280;margin-top:1.2rem}}
</style>
{body}"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body))


@app.get("/", response_class=HTMLResponse)
def index():
    s = _store()
    callback.sweep(s)                     # expire anything past its deadline
    open_qs = callback.pending(s)
    if not open_qs:
        return _page("All clear",
                     "<h1>All clear</h1>"
                     "<p class='sub'>NANI isn't stuck on anything right now.</p>"
                     "<p class='empty'>This page will have something on it when a "
                     "schedule conflict can't be resolved without your say-so - a hard "
                     "deadline with no safe window, say.</p>")
    items = "".join(
        f"<div class='card'><p class='q'>{html.escape(q.question)}</p>"
        f"<a href='/q/{q.id}'>Sort this out &rarr;</a></div>" for q in open_qs)
    return _page("NANI needs a decision",
                 f"<h1>{len(open_qs)} thing(s) NANI can't decide alone</h1>"
                 "<p class='sub'>Your schedule hit a conflict NANI won't guess its way "
                 "through. Your answer picks up planning right where it stopped.</p>" + items)


@app.get("/q/{qid}", response_class=HTMLResponse)
def show(qid: str):
    q = _store().get_question(qid)
    if q is None:
        return _page("Not found", "<h1>Not found</h1><p class='sub'>No such question.</p>")
    if q.is_answered:
        return _page("Already sorted",
                     "<h1>Already sorted</h1><p class='sub'>This one's already been "
                     "answered &mdash; answers are recorded once and never overwritten.</p>"
                     f"<div class='ctx'><b>What you said</b>{html.escape(q.answer or '')}</div>"
                     "<p><a href='/'>Back</a></p>")
    ctx = ""
    for k, v in (q.context or {}).items():
        if k == "resume_state":
            continue
        ctx += (f"<div class='ctx'><b>{html.escape(str(k).replace('_',' '))}</b>"
                f"{html.escape(str(v))}</div>")
    return _page("A call only you can make",
                 f"<h1>A call only you can make</h1>"
                 "<p class='sub'>NANI found a conflict it can't resolve without changing "
                 "something you didn't ask it to touch. Tell it what to do - move the task, "
                 "shorten it, push the deadline, drop it, or override the protected time. "
                 "Say plainly if you're not sure &mdash; that's a useful answer too.</p>"
                 f"<p class='q'>{html.escape(q.question)}</p>{ctx}"
                 f"<form method='post' action='/q/{q.id}'>"
                 "<textarea name='answer' autofocus placeholder='What should NANI do&hellip;'></textarea>"
                 "<input type='hidden' name='who' value='user'>"
                 "<button type='submit'>Send</button></form>"
                 "<p class='note'>Recorded as your decision, kept separate from what NANI "
                 "already proposed.</p>")


@app.post("/q/{qid}")
def submit(qid: str, answer: str = Form(...), who: str = Form("user")):
    text = (answer or "").strip()
    if not text:
        return RedirectResponse(f"/q/{qid}", status_code=303)
    callback.answer(_store(), qid, text, who=who)
    return RedirectResponse("/thanks", status_code=303)


@app.get("/thanks", response_class=HTMLResponse)
def thanks():
    return _page("Got it",
                 "<h1>Got it</h1><p class='sub'>NANI has resumed planning with your answer.</p>"
                 "<p><a href='/'>Anything else waiting?</a></p>")
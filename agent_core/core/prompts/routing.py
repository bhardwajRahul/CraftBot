# -*- coding: utf-8 -*-
"""
Session routing prompts for agent_core.

This module contains prompt templates for routing messages to sessions.
"""

# --- Unified Session Routing ---
# This prompt is the LAST-RESORT routing decision. The chat handler short-circuits
# the easy cases (explicit UI reply target, third-party notifications, reply
# markers) before this prompt runs.
#
# The prompt's job: with one or more active tasks, decide whether the incoming
# message is unambiguously linked to one of them (continuation, modification,
# cancellation, answer to its question, or Living UI reference) or is a fresh
# request that deserves a new session. Default to NEW when in doubt.
#
# A waiting task's approval-seeking question ("is this acceptable?") plus a
# user reply containing approval language ("thanks", "looks good") IS the
# task_end signal that task is parked for — the prompt is explicit about this
# so the LLM does not misfile it as conversational chatter.
ROUTE_TO_SESSION_PROMPT = """
<objective>
You are a session router. Decide whether an incoming message is a clear continuation
of an existing task, or a new request that should open a new session.
</objective>

<incoming_item>
Type: {item_type}
Content: {item_content}
Source Platform: {source_platform}
User's current Living UI page: {current_living_ui_id}
</incoming_item>

<existing_sessions>
{existing_sessions}
</existing_sessions>

<recent_conversation>
Recent messages across all sessions (oldest first, may include completed tasks
that are no longer in <existing_sessions>):
{recent_conversation}
</recent_conversation>

<rules>
DEFAULT: new session. Route to an existing session S ONLY when the message
has an unambiguous link to S.

Route to S when the message:
1. Names an artifact / file / output S produced.
2. Modifies, narrows, or cancels S's instruction.
3. Answers a question S's last agent message asked. Critical case: if S is
   WAITING FOR REPLY and its last outbound sought approval or change
   feedback (e.g. "is this acceptable?", "does this look good?", "want
   changes?"), then approval phrases — "thanks", "looks good", "it's good",
   "done", "that's all", including thanks-wrapped variants like
   "thanks, looks good" or "thanks for X, it's good" — ARE that answer.
   This is the task_end approval S is parked for; do not misclassify as
   conversational.
4. Living UI: context-free reference ("fix this", "it broke") AND S's
   Living UI ID matches the user's current page; OR the message explicitly
   names a Living UI matching S's binding (chat is global, any page).

Insufficient → new session:
- S exists, or is the only active task.
- Same topic as S without an explicit reference.
- S's last outbound is only a generic close-out ("anything else?",
  "let me know if needed") — close-outs are not routable questions; an
  unrelated follow-up is a new session.

recent_conversation resolves ambiguous references. If the relevant topic is
in a COMPLETED task (absent from existing_sessions), choose NEW —
completed sessions cannot resume.
</rules>

<output_format>
Return ONLY a valid JSON object:
- Route to existing: {{ "reason": "<brief>", "action": "route", "session_id": "<session_id>" }}
- Create new: {{ "reason": "<brief>", "action": "new", "session_id": "new" }}
</output_format>
"""

__all__ = [
    "ROUTE_TO_SESSION_PROMPT",
]

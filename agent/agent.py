"""The ReAct loop for a single case.

LLM -> structured tool call -> runtime validation -> HTTP -> observation -> LLM.
The loop owns nothing safety critical; it just shuttles messages and enforces the
step/stagnation limits by delegating to the runtime and, when stuck, forcing a
final constrained escalation turn.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import config
from .prompts import FORCED_TERMINATION_MESSAGE, SYSTEM_PROMPT, initial_user_message
from .runtime import Runtime
from .tools import anthropic_tools
from .world_client import WorldClient

# cap observation text so a giant transcript can't blow up context
_MAX_OBS_CHARS = 6000


def _trim(obs: dict) -> str:
    """Serialize an observation and hard-cap its length so a giant transcript or
    document can't blow up the context window."""
    text = json.dumps(obs, default=str)
    if len(text) > _MAX_OBS_CHARS:
        text = text[:_MAX_OBS_CHARS] + " ...[truncated]"
    return text


class CaseAgent:
    def __init__(self, client, model: str, world: WorldClient, case_id: str, run_id: str,
                 verbose: bool = True):
        self.client = client
        self.model = model
        self.runtime = Runtime(world, case_id, run_id)
        self.case_id = case_id
        self.verbose = verbose
        # Anthropic keeps the system prompt out of the message list; messages are
        # only user/assistant turns.
        self.messages: list[dict[str, Any]] = [
            {"role": "user", "content": initial_user_message(case_id)},
        ]

    def _say(self, *a):
        if self.verbose:
            print(f"[{self.case_id}]", *a, flush=True)

    async def _complete(self, tool_choice: dict | None = None):
        # NB: temperature is intentionally not sent — it is deprecated/rejected on the
        # current Claude models. Determinism now comes from the model default, not a knob.
        return await self.client.messages.create(
            model=self.model,
            system=SYSTEM_PROMPT,
            messages=self.messages,
            tools=anthropic_tools(),
            tool_choice=tool_choice or {"type": "auto"},
            max_tokens=config.MAX_TOKENS,
        )

    async def _complete_with_retry(self, tool_choice: dict | None = None, attempts: int = 3):
        """Call the model, retrying a few times on transient API errors before
        giving up. The caller escalates the case if this ultimately raises."""
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await self._complete(tool_choice)
            except Exception as exc:
                last_exc = exc
                self._say(f"LLM call error (attempt {i + 1}/{attempts}):", exc)
                if i < attempts - 1:
                    await asyncio.sleep(2 * (i + 1))
        raise last_exc

    @staticmethod
    def _assistant_content(msg) -> list[dict]:
        """Flatten a response's content blocks into the plain-dict form we feed back."""
        content: list[dict] = []
        for b in msg.content:
            if b.type == "text":
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        # Never hand back an empty assistant turn — the API rejects it next call.
        return content or [{"type": "text", "text": ""}]

    async def run(self) -> dict:
        while self.runtime.state.terminal_state is None:
            stuck = self.runtime.is_stuck()
            if stuck:
                self._say("forced termination:", stuck)
                await self._forced_escalation(stuck)
                break

            try:
                resp = await self._complete_with_retry()
            except Exception as exc:
                # The model call kept failing — escalate
                # deterministically with what we have so the queue still terminates.
                self._say("LLM call failed after retries, escalating:", exc)
                await self.runtime.force_escalate_from_history(f"LLM call failed: {exc}")
                break

            self.messages.append({"role": "assistant", "content": self._assistant_content(resp)})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]

            if not tool_uses:
                # Model produced prose instead of acting; nudge it back to a tool.
                self.messages.append({
                    "role": "user",
                    "content": "Respond only by calling exactly one tool.",
                })
                self.runtime.state.no_progress_streak += 1
                continue

            results: list[dict] = []
            terminal = False
            for tu in tool_uses:
                args = tu.input if isinstance(tu.input, dict) else {}
                self._say("->", tu.name, args)
                obs = await self.runtime.execute(tu.name, args)
                self._say("   ", obs.get("note") or obs.get("error"))
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": _trim(obs)})
                if obs.get("terminal"):
                    terminal = True
                    break
            self.messages.append({"role": "user", "content": results})
            if terminal:
                break

        return self.report()

    async def _forced_escalation(self, reason: str) -> None:
        """One constrained turn asking for an escalation; deterministic fallback if it fails."""
        self.messages.append({"role": "user", "content": FORCED_TERMINATION_MESSAGE})
        try:
            resp = await self._complete(tool_choice={"type": "tool", "name": "escalate_case"})
            self.messages.append({"role": "assistant", "content": self._assistant_content(resp)})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if tool_uses:
                tu = tool_uses[0]
                args = tu.input if isinstance(tu.input, dict) else {}
                obs = await self.runtime._do_escalate_case(args, lenient=True)
                self.messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": _trim(obs)}],
                })
                if obs.get("terminal"):
                    self._say("escalated via final model turn")
                    return
        except Exception as exc:
            self._say("forced escalation model turn failed:", exc)
        # Deterministic last resort: build the package from history.
        await self.runtime.force_escalate_from_history(reason)
        self._say("escalated via deterministic fallback")

    def report(self) -> dict:
        s = self.runtime.state
        return {
            "case_id": self.case_id,
            "terminal_state": s.terminal_state,
            "steps": s.step_count,
            "terminal_attempts": s.terminal_attempts,
            "evidence_count": len(s.fetched_artifacts),
            "action_history": s.action_history,
        }

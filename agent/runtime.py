"""The deterministic runtime.

This is where all safety critical execution lives. It validates tool
arguments, executes them against the world, tracks evidence, distrusts the
directory, controls the simulated clock, guards against stuck loops, and gates
the terminal decision. It never judges clinical meaning or business correctness :
that is the LLM's job.

The single `CaseState` object is also the audit trail: any terminal decision can
be reconstructed from `action_history` + `evidence_index`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from pydantic import ValidationError

from . import config
from .tools import TOOL_MODELS
from .world_client import WorldClient


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


@dataclass
class CaseState:
    case_id: str
    run_id: str
    step_count: int = 0
    terminal_attempts: int = 0
    terminal_state: Optional[str] = None            # "resolved" | "escalated"
    no_progress_streak: int = 0

    document_refs: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)

    fetched_artifacts: set[str] = field(default_factory=set)   # evidence_ids seen
    evidence_index: dict[str, str] = field(default_factory=dict)  # id -> short desc
    action_history: list[dict] = field(default_factory=list)
    inbox_seen_ids: set[int] = field(default_factory=set)

    resubmit_attempts: int = 0
    # True once the agent has engaged a party through a channel (a connected call, a
    # fax, or a text). This is the mechanical proxy for "the agent did work that could
    # have changed the claim's situation" — pure reading/investigation doesn't count.
    outbound_action_taken: bool = False
    # Consecutive time advances that delivered nothing. Bounds how long we call waiting
    # "productive" before telling the agent the reply may never come.
    empty_advances: int = 0
    # the claim-probe gate may fire at most once, so it can never trap a case
    _claim_probe_gate_fired: bool = False

    # dedup bookkeeping
    _seen_docs: set[str] = field(default_factory=set)
    _seen_policies: set[str] = field(default_factory=set)
    _seen_transcripts: set[str] = field(default_factory=set)
    _sent_texts: set[str] = field(default_factory=set)
    _sent_faxes: set[str] = field(default_factory=set)
    _claim_status: dict[str, str] = field(default_factory=dict)
    _call_seq: int = 0
    _chan_seq: int = 0


class Runtime:
    """Executes one tool call against the world and returns an observation dict."""

    def __init__(self, world: WorldClient, case_id: str, run_id: str):
        self.world = world
        self.state = CaseState(case_id=case_id, run_id=run_id)

    # -- helpers ------------------------------------------------------------
    def _register(self, token: str, desc: str) -> None:
        self.state.fetched_artifacts.add(token)
        self.state.evidence_index[token] = desc

    def _log(self, tool: str, args: dict, outcome: str) -> None:
        self.state.action_history.append({
            "step": self.state.step_count,
            "tool": tool,
            "args": args,
            "outcome": outcome,
        })

    def _obs(self, progress: bool, note: str, **extra) -> dict:
        if progress:
            self.state.no_progress_streak = 0
        else:
            self.state.no_progress_streak += 1
        out = {"note": note}
        out.update(extra)
        return out

    def is_stuck(self) -> Optional[str]:
        if self.state.step_count >= config.STEP_BUDGET:
            return f"step budget of {config.STEP_BUDGET} reached"
        if self.state.no_progress_streak >= config.STAGNATION_CAP:
            return f"{config.STAGNATION_CAP} consecutive actions produced no new information"
        return None

    # -- dispatch -----------------------------------------------------------
    async def execute(self, tool: str, raw_args: dict) -> dict:
        """Validate + run a single tool call. Always returns an observation dict."""
        if tool not in TOOL_MODELS:
            return {"error": f"unknown tool '{tool}'"}

        # Schema validation before anything touches HTTP.
        try:
            args = TOOL_MODELS[tool](**(raw_args or {})).model_dump()
        except ValidationError as exc:
            return {"error": "invalid tool arguments", "detail": json.loads(exc.json())}

        self.state.step_count += 1

        handler = getattr(self, f"_do_{tool}")
        try:
            return await handler(args)
        except Exception as exc:  # defensive: no single tool call crashes the run
            self._log(tool, args, f"runtime exception: {exc}")
            return self._obs(False, f"the runtime hit an unexpected error running {tool}: {exc}")

    # -- reads --------------------------------------------------------------
    async def _do_get_case(self, args: dict) -> dict:
        cid = self.state.case_id
        r = await self.world.get_case(cid)
        if not r["ok"]:
            self._log("get_case", args, f"error: {r['error']}")
            return self._obs(False, f"could not read case: {r['error']}")
        data = r["data"]
        self.state.document_refs = data.get("documents", [])
        self.state.claim_ids = data.get("claims", [])
        first = "case:" + cid not in self.state.fetched_artifacts
        self._register(f"case:{cid}", "the case file (advocate summary + attachments index)")
        self._log("get_case", args, f"read; {len(self.state.document_refs)} docs, {len(self.state.claim_ids)} claims")
        return self._obs(
            first,
            "Case file read. The 'Where things stand' note is a lead, not ground truth.",
            case_file=data.get("case_file"),
            goal=data.get("goal"),
            document_refs=self.state.document_refs,
            claim_ids=self.state.claim_ids,
            status=data.get("status"),
            evidence_ids=[f"case:{cid}"],
        )

    async def _do_read_document(self, args: dict) -> dict:
        ref = args["ref"]
        # The model sometimes passes an evidence token (inbox:227, call:3, fax-sent:1)
        # here. Those are not case documents — their content was already returned to the
        # model when the event occurred. Redirect instead of failing with an opaque "no
        # document" error, which otherwise wastes steps retrying the wrong tool.
        if ref.startswith(("inbox:", "call:", "fax-sent:", "text-sent:", "resubmit:")):
            return self._obs(False,
                             f"'{ref}' is an event/inbox reference, not a case document. Its "
                             "content was already returned to you when it landed (advance_time "
                             "or check_inbox); call check_inbox to see current items again. "
                             f"read_document is only for this case's attached documents: "
                             f"{self.state.document_refs}.")
        if ref.startswith("claim:"):
            return self._obs(False,
                             f"'{ref}' is a claim reference — read it with get_claim, not "
                             "read_document.")
        ref = ref.split("doc:", 1)[-1]  # tolerate a stray "doc:" prefix on a real ref
        token = f"doc:{ref}"
        if ref in self.state._seen_docs:
            self._log("read_document", args, "already read (cached)")
            return self._obs(False, f"'{ref}' was already read earlier — see prior observation.",
                             evidence_ids=[token])
        r = await self.world.get_document(self.state.case_id, ref)
        if not r["ok"]:
            self._log("read_document", args, f"error: {r['error']}")
            return self._obs(False, f"could not read document '{ref}': {r['error']}")
        self.state._seen_docs.add(ref)
        self._register(token, f"document '{ref}'")
        self._log("read_document", args, "read")
        return self._obs(True, f"document '{ref}' read.",
                         content=r["data"].get("content"), evidence_ids=[token])

    async def _do_list_policy(self, args: dict) -> dict:
        r = await self.world.list_policy()
        if not r["ok"]:
            self._log("list_policy", args, f"error: {r['error']}")
            return self._obs(False, f"could not list policy: {r['error']}")
        self._log("list_policy", args, f"{len(r['data'])} docs")
        return self._obs(True, "Coverage policy documents available.", policy_docs=r["data"])

    async def _do_read_policy(self, args: dict) -> dict:
        name = args["name"]
        token = f"policy:{name}"
        if name in self.state._seen_policies:
            self._log("read_policy", args, "already read (cached)")
            return self._obs(False, f"policy '{name}' was already read earlier.", evidence_ids=[token])
        r = await self.world.get_policy(name)
        if not r["ok"]:
            self._log("read_policy", args, f"error: {r['error']}")
            return self._obs(False, f"could not read policy '{name}': {r['error']}")
        self.state._seen_policies.add(name)
        self._register(token, f"policy '{name}'")
        self._log("read_policy", args, "read")
        return self._obs(True, f"policy '{name}' read.",
                         content=r["data"].get("content"), evidence_ids=[token])

    async def _do_get_directory(self, args: dict) -> dict:
        r = await self.world.get_directory()
        if not r["ok"]:
            self._log("get_directory", args, f"error: {r['error']}")
            return self._obs(False, f"could not read directory: {r['error']}")
        first = "directory" not in self.state.fetched_artifacts
        self._register("directory", "the ops directory (numbers may be stale)")
        self._log("get_directory", args, "read")
        return self._obs(first, "Directory read. Treat numbers as possibly stale.",
                         directory=r["data"], evidence_ids=["directory"])

    async def _do_get_claim(self, args: dict) -> dict:
        claim_id = args["claim_id"]
        r = await self.world.get_claim(claim_id)
        if not r["ok"]:
            self._log("get_claim", args, f"error: {r['error']}")
            return self._obs(False, f"could not read claim {claim_id}: {r['error']}")
        remittance = r["data"].get("remittance", "")
        token = f"claim:{claim_id}"
        changed = self.state._claim_status.get(claim_id) != remittance
        self.state._claim_status[claim_id] = remittance
        self._register(token, f"claim {claim_id} remittance/status")
        self._log("get_claim", args, "read" + ("" if changed else " (unchanged)"))
        return self._obs(changed, f"claim {claim_id} status read.",
                         remittance=remittance, evidence_ids=[token])

    # -- actions ------------------------------------------------------------
    async def _do_resubmit_claim(self, args: dict) -> dict:
        claim_id = args["claim_id"]
        self.state.resubmit_attempts += 1
        r = await self.world.resubmit_claim(claim_id)
        if not r["ok"]:
            self._log("resubmit_claim", args, f"error: {r['error']}")
            return self._obs(False, f"could not resubmit claim {claim_id}: {r['error']}")
        data = r["data"]
        self.state._chan_seq += 1
        token = f"resubmit:{claim_id}:{self.state._chan_seq}"
        if not data.get("accepted"):
            self._register(token, f"resubmit of {claim_id}: rejected")
            self._log("resubmit_claim", args, "nothing to resubmit")
            return self._obs(False,
                             "Resubmission not accepted — there is nothing to resubmit, "
                             "meaning the underlying problem hasn't been fixed yet.",
                             detail=data.get("detail"), evidence_ids=[token])
        self.state.empty_advances = 0
        self._register(token, f"resubmit of {claim_id}: accepted, lands ~day {data.get('expected_day')}")
        self._log("resubmit_claim", args, f"accepted, expected day {data.get('expected_day')}")
        return self._obs(True,
                         "Resubmission accepted. The result lands later in the inbox — "
                         "advance time and read it before concluding anything.",
                         expected_day=data.get("expected_day"), evidence_ids=[token])

    async def _do_place_call(self, args: dict) -> dict:
        to = args["to"]
        purpose = args.get("purpose")
        r = await self.world.place_call(self.state.case_id, to, purpose)
        if not r["ok"]:
            self._log("place_call", args, f"error: {r['error']}")
            return self._obs(False, f"call failed: {r['error']}")
        data = r["data"]
        outcome = data.get("outcome", "no_answer")
        party = data.get("party")
        transcript = data.get("transcript")
        digits = _digits(to)
        if outcome == "connected":
            self.state.outbound_action_taken = True
        self.state._call_seq += 1
        token = f"call:{self.state._call_seq}"
        self._register(token, f"call to {to} ({party or 'unknown'}) — outcome {outcome}")
        self._log("place_call", args, f"{outcome} ({party})")
        # Transport success != business success: only a connected call with a
        # transcript we haven't seen counts as progress.
        tkey = f"{digits}|{transcript}"
        progress = outcome == "connected" and tkey not in self.state._seen_transcripts
        if transcript:
            self.state._seen_transcripts.add(tkey)
        if outcome == "connected" and not progress:
            note = ("This call returned the SAME conversation as a previous call to this "
                    "number — no new information. Stop re-calling it; take a different action.")
        elif outcome == "connected":
            note = ("Call connected. Read the transcript to decide whether you reached the "
                    "right party and what it tells you.")
        else:
            note = (f"Call outcome: {outcome}. You did not reach a person. Try another number "
                    "listed for this party or a different channel; if the party the case "
                    "points to cannot be reached, that is a blocker you can escalate.")
        return self._obs(progress, note, outcome=outcome, party=party,
                         transcript=transcript, evidence_ids=[token])

    async def _do_send_fax(self, args: dict) -> dict:
        to = args["to"]
        docs = sorted(args.get("documents") or [])
        note = args.get("note")
        # validate refs exist (world would 400; catch early with a clearer message)
        unknown = [d for d in docs if d not in set(self.state.document_refs)]
        if self.state.document_refs and unknown:
            self._log("send_fax", args, f"unknown docs {unknown}")
            return self._obs(False,
                             f"these refs aren't on this case: {unknown}. "
                             f"Valid refs: {self.state.document_refs}")
        # Key on digits when present, else the normalized name — otherwise every
        # name-addressed fax collapses to the same key and a legitimate re-fax
        # (e.g. after a priming call unlocked the right party) is wrongly blocked
        # as a duplicate. Mirrors the target keying in _do_place_call.
        key = f"{_digits(to) or to.strip().lower()}|{','.join(docs)}"
        r = await self.world.send_fax(self.state.case_id, to, docs, note)
        if not r["ok"]:
            self._log("send_fax", args, f"error: {r['error']}")
            return self._obs(False, f"fax failed: {r['error']}")
        first = key not in self.state._sent_faxes
        self.state._sent_faxes.add(key)
        self.state.outbound_action_taken = True
        self.state.empty_advances = 0     # fresh thing to wait for
        self.state._chan_seq += 1
        token = f"fax-sent:{self.state._chan_seq}"
        self._register(token, f"fax to {to} with {docs or 'no docs'}")
        self._log("send_fax", args, "sent" if first else "re-sent (duplicate)")
        note_txt = (
            "Fax reported delivered. Whether anyone works that queue is unknown — any reply "
            "lands later in the inbox; advance time to find out."
            if first else
            "You already sent this exact fax. Re-sending won't help unless something changed "
            "(e.g. a call unlocked the right party). Consider waiting or another path."
        )
        return self._obs(first, note_txt, evidence_ids=[token])

    async def _do_send_text(self, args: dict) -> dict:
        body = args["body"]
        r = await self.world.send_text(self.state.case_id, body)
        if not r["ok"]:
            self._log("send_text", args, f"error: {r['error']}")
            return self._obs(False, f"text failed: {r['error']}")
        data = r["data"]
        self.state.outbound_action_taken = True
        self.state._chan_seq += 1
        token = f"text-sent:{self.state._chan_seq}"
        self._register(token, "text to patient")
        reply = data.get("reply")
        first = body not in self.state._sent_texts
        self.state._sent_texts.add(body)
        self._log("send_text", args, "sent" + (" (immediate reply)" if reply else ""))
        if reply:
            return self._obs(True, "Text sent; the patient replied immediately.",
                             reply=reply, evidence_ids=[token])
        self.state.empty_advances = 0
        return self._obs(first,
                         "Text sent. A reply, if it comes, lands later in the inbox — "
                         "advance time and check.",
                         evidence_ids=[token])

    async def _do_check_inbox(self, args: dict) -> dict:
        r = await self.world.get_inbox(self.state.case_id)
        if not r["ok"]:
            self._log("check_inbox", args, f"error: {r['error']}")
            return self._obs(False, f"could not read inbox: {r['error']}")
        items = r["data"]
        new = [i for i in items if i["id"] not in self.state.inbox_seen_ids]
        for i in items:
            self.state.inbox_seen_ids.add(i["id"])
            self._register(f"inbox:{i['id']}", f"inbox item {i['id']} ({i['kind']}, day {i['day']})")
        self._log("check_inbox", args, f"{len(items)} total, {len(new)} new")
        return self._obs(bool(new),
                         f"Inbox has {len(items)} item(s), {len(new)} new since you last looked.",
                         items=items,
                         evidence_ids=[f"inbox:{i['id']}" for i in items])

    async def _do_advance_time(self, args: dict) -> dict:
        clock = await self.world.get_clock()
        if not clock["ok"]:
            self._log("advance_time", args, f"clock error: {clock['error']}")
            return self._obs(False, f"could not read clock: {clock['error']}")
        day = clock["data"]["day"]
        pending_claims = clock["data"].get("pending", [])

        # Prefer advancing exactly to the next known (claim) event; otherwise take an
        # adaptive step that grows off empty_advances (the wait counter) so a reply that
        # lands within ~9 days is reached within the tolerance window.
        future = [p["due_day"] for p in pending_claims if p["due_day"] > day]
        if future:
            step = min(future) - day
        else:
            step = min(config.MIN_ADVANCE + 2 * self.state.empty_advances, config.MAX_ADVANCE)
        step = max(config.MIN_ADVANCE, step)

        r = await self.world.advance_clock(step)
        if not r["ok"]:
            self._log("advance_time", args, f"error: {r['error']}")
            return self._obs(False, f"could not advance clock: {r['error']}")
        data = r["data"]
        delivered = data.get("delivered", [])
        new = [d for d in delivered if d["id"] not in self.state.inbox_seen_ids]
        for d in delivered:
            self.state.inbox_seen_ids.add(d["id"])
            self._register(f"inbox:{d['id']}", f"inbox item {d['id']} ({d['kind']}, day {d['day']})")
        self._log("advance_time", args, f"->day {data['day']}, {len(new)} delivered")
        if new:
            self.state.empty_advances = 0
            return self._obs(True,
                             f"Advanced to day {data['day']}. {len(new)} new item(s) landed.",
                             day=data["day"], delivered=new,
                             evidence_ids=[f"inbox:{d['id']}" for d in delivered])

        # Nothing landed. The clock only reports pending *claims*, so a fax/text/scheduled
        # reply is invisible to it — tolerate a few empty advances as legitimate waiting
        # (not stagnation), then tell the agent the reply may never come.
        self.state.empty_advances += 1
        if self.state.empty_advances <= config.EMPTY_ADVANCE_TOLERANCE:
            return self._obs(True,
                             f"Advanced to day {data['day']}. Nothing landed yet — a reply you're "
                             "waiting on may still be in flight, so advancing again is reasonable. "
                             "If several more bring nothing, it may not be coming.",
                             day=data["day"])
        return self._obs(False,
                         f"Advanced to day {data['day']}. Nothing has landed after "
                         f"{self.state.empty_advances} advances. The world cannot promise a fax "
                         "or text will ever be answered — take a different action or, if nothing "
                         "else can move this case, end it.",
                         day=data["day"])

    # -- terminal gate ------------------------------------------------------
    def _check_evidence(self, evidence: list[str]) -> list[str]:
        """Return the subset of cited ids that were never actually observed."""
        return [e for e in evidence if e not in self.state.fetched_artifacts]

    def _claim_probe_block(self, terminal: str) -> Optional[str]:
        """Block a claim case that did corrective work but never probed resubmit_claim.

        This targets one specific hallucination: the agent takes actions toward fixing
        a claim (engages a party by call/fax/text) and then resolves or escalates
        WITHOUT resubmitting — e.g. assuming the supplier must rebill. It deliberately
        does NOT fire when the agent only investigated and found an irreducible blocker
        (no outbound action), so a genuine 'nothing could be done' escalation is never
        blocked. Mechanical ('did you probe after doing work') not a judgment on the
        conclusion, and it fires at most once, so it can never trap a case.
        """
        if self.state.claim_ids and self.state.outbound_action_taken \
                and not self.state.resubmit_attempts and not self.state._claim_probe_gate_fired:
            self.state._claim_probe_gate_fired = True
            return (
                f"{terminal} rejected: this case has claim(s) {self.state.claim_ids}, you have "
                "taken actions toward fixing it, but you have never called resubmit_claim. "
                "Resubmitting is the only thing that re-adjudicates a claim: once the underlying "
                "problem is genuinely fixed, call resubmit_claim, advance time, and read the "
                "result from the inbox. If the fix is not actually in place it comes back "
                "'nothing to resubmit' or denies again — which tells you a prerequisite is still "
                "missing (find it) rather than that the case is unresolvable. Attempt the "
                "resubmission and see its result before ending this case."
            )
        return None

    async def _do_resolve_case(self, args: dict) -> dict:
        self.state.terminal_attempts += 1
        evidence = args["evidence"]
        rationale = (args.get("rationale") or "").strip()
        if not evidence:
            return self._obs(False,
                             "RESOLVE rejected: you must cite at least one evidence_id showing "
                             "the blocker is gone. If you can't, this should be an escalation.")
        bad = self._check_evidence(evidence)
        if bad:
            return self._obs(False,
                             f"RESOLVE rejected: these evidence_ids were never observed in this "
                             f"trajectory: {bad}. Cite only ids the runtime returned to you.")
        if not rationale:
            return self._obs(False, "RESOLVE rejected: rationale is required.")
        blocked = self._claim_probe_block("RESOLVE")
        if blocked:
            return self._obs(False, blocked)
        summary = args["summary"] + f"\n\nWhy resolve (not escalate): {rationale}"
        r = await self.world.resolve(self.state.case_id, summary, evidence)
        if not r["ok"]:
            return self._obs(False, f"RESOLVE call failed at the world: {r['error']}")
        self.state.terminal_state = "resolved"
        self._log("resolve_case", {"evidence": evidence}, "RESOLVED")
        return {"note": "Case RESOLVED.", "terminal": True, "status": "resolved"}

    async def _do_escalate_case(self, args: dict, lenient: bool = False) -> dict:
        self.state.terminal_attempts += 1
        evidence = args.get("evidence") or []
        rationale = (args.get("rationale") or "").strip()
        bad = self._check_evidence(evidence)
        if bad and not lenient:
            return self._obs(False,
                             f"ESCALATE rejected: these evidence_ids were never observed: {bad}. "
                             "Cite only ids you actually saw (evidence may also be empty).")
        evidence = [e for e in evidence if e not in bad]  # drop unverifiable ids
        if not rationale and not lenient:
            return self._obs(False, "ESCALATE rejected: rationale is required.")
        if not lenient:
            blocked = self._claim_probe_block("ESCALATE")
            if blocked:
                return self._obs(False, blocked)
        package = args["package"]
        if evidence:
            package += "\n\nEvidence ids: " + ", ".join(evidence)
        if rationale:
            package += f"\n\nWhy escalate (not resolve): {rationale}"
        r = await self.world.escalate(self.state.case_id, args["reason"], package)
        if not r["ok"]:
            return self._obs(False, f"ESCALATE call failed at the world: {r['error']}")
        self.state.terminal_state = "escalated"
        self._log("escalate_case", {"evidence": evidence}, "ESCALATED")
        return {"note": "Case ESCALATED.", "terminal": True, "status": "escalated"}

    # -- forced fallback ----------------------------------------------------
    async def force_escalate_from_history(self, reason: str) -> dict:
        """Last-resort deterministic escalation built from the action log.

        This is the TRUE last resort — there is no fallback after it, so it escalates
        leniently (evidence/rationale/claim-probe checks skipped) and is guaranteed to
        terminate. A case can never be left non-terminal here.
        """
        tried = [f"- {a['tool']}({a.get('args')}) -> {a['outcome']}" for a in self.state.action_history]
        package = (
            f"AUTO-GENERATED HANDOFF (agent forced to terminate: {reason}).\n\n"
            f"Case {self.state.case_id}. The agent could not reach a confident resolution "
            f"within its action limits.\n\n"
            f"Actions taken and outcomes:\n" + "\n".join(tried) + "\n\n"
            f"Evidence gathered: {sorted(self.state.fetched_artifacts)}\n\n"
            "Recommended next step: a human advocate should review the trajectory above, "
            "confirm the current blocker, and continue from the last productive action."
        )
        args = {"reason": f"Agent auto-escalated: {reason}", "package": package,
                "evidence": sorted(self.state.fetched_artifacts), "rationale": reason}
        return await self._do_escalate_case(args, lenient=True)

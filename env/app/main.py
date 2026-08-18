"""The world your agent works in.

Every world-touching call takes an optional `X-Run-Id` header. Runs are isolated:
use a fresh id to start clean, or POST /runs/{id}/reset.
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import world
from .state import RUNS, Ending

app = FastAPI(
    title="Working the Queue",
    description=__doc__,
    version="1.0.0",
)


def run_of(run_id: Optional[str]):
    return RUNS.get(run_id or "default")


class CallRequest(BaseModel):
    case_id: str
    to: str = Field(description="Phone number, or the name of who you're calling.")
    purpose: Optional[str] = Field(default=None, description="Free text. Recorded, not interpreted.")


class FaxRequest(BaseModel):
    case_id: str
    to: str
    documents: list[str] = Field(default_factory=list, description="Document refs from the case.")
    note: Optional[str] = None


class TextRequest(BaseModel):
    case_id: str
    body: str


class AdvanceRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=90)


class ResolveRequest(BaseModel):
    summary: str
    evidence: list[str] = Field(default_factory=list)


class EscalateRequest(BaseModel):
    reason: str
    package: str


def _require_case(case_id: str) -> dict:
    found = world.case(case_id)
    if not found:
        raise HTTPException(404, f"no case {case_id}")
    return found


def _require_open(run, case_id: str) -> None:
    if case_id in run.endings:
        raise HTTPException(409, f"{case_id} already ended as {run.endings[case_id].outcome}")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"ok": True, "cases": len(world.case_ids())}


@app.get("/cases", tags=["cases"])
def list_cases(x_run_id: Optional[str] = Header(default=None)) -> list[dict]:
    run = run_of(x_run_id)
    out = []
    for case_id in world.case_ids():
        entry = world.case(case_id)
        ending = run.endings.get(case_id)
        out.append({
            "case_id": case_id,
            "patient": entry["patient"],
            "goal": entry["goal"],
            "status": ending.outcome if ending else "open",
        })
    return out


@app.get("/cases/{case_id}", tags=["cases"])
def get_case(case_id: str, x_run_id: Optional[str] = Header(default=None)) -> dict:
    entry = _require_case(case_id)
    run = run_of(x_run_id)
    ending = run.endings.get(case_id)
    return {
        "case_id": case_id,
        "patient": entry["patient"],
        "goal": entry["goal"],
        "status": ending.outcome if ending else "open",
        "case_file": world.case_file(case_id),
        "documents": sorted((entry.get("documents") or {}).keys()),
        "claims": sorted((entry.get("claims") or {}).keys()),
    }


@app.get("/cases/{case_id}/documents/{ref}", tags=["cases"])
def get_document(case_id: str, ref: str, x_run_id: Optional[str] = Header(default=None)) -> dict:
    entry = _require_case(case_id)
    content = (entry.get("documents") or {}).get(ref)
    if content is None:
        raise HTTPException(404, f"no document {ref} on {case_id}")
    run_of(x_run_id).log(case_id, "document_read", {"ref": ref})
    return {"case_id": case_id, "ref": ref, "content": content}


@app.get("/directory", tags=["reference"])
def get_directory(x_run_id: Optional[str] = Header(default=None)) -> list[dict]:
    run_of(x_run_id).log("-", "directory_read", {})
    return world.directory()


@app.get("/policy", tags=["reference"])
def list_policy() -> list[str]:
    return list(world.policy_docs())


@app.get("/policy/{name}", tags=["reference"])
def get_policy(name: str, x_run_id: Optional[str] = Header(default=None)) -> dict:
    content = world.policy(name)
    if content is None:
        raise HTTPException(404, f"no policy document {name}")
    run_of(x_run_id).log("-", "policy_read", {"name": name})
    return {"name": name, "content": content}


@app.get("/claims/{claim_id}", tags=["claims"])
def get_claim(claim_id: str, x_run_id: Optional[str] = Header(default=None)) -> dict:
    found = world.find_claim(claim_id)
    if not found:
        raise HTTPException(404, f"no claim {claim_id}")
    case_id, entry = found
    run = run_of(x_run_id)
    run.log(case_id, "claim_status", {"claim_id": claim_id})
    return {"claim_id": claim_id, "case_id": case_id, "remittance": entry["status"]}


@app.post("/claims/{claim_id}/resubmit", tags=["claims"])
def resubmit_claim(claim_id: str, x_run_id: Optional[str] = Header(default=None)) -> dict:
    found = world.find_claim(claim_id)
    if not found:
        raise HTTPException(404, f"no claim {claim_id}")
    case_id, _ = found
    run = run_of(x_run_id)
    option = world.resubmission(case_id, claim_id, run.known(case_id))
    run.log(case_id, "claim_resubmit", {"claim_id": claim_id})
    if not option:
        return {"accepted": False, "detail": "Nothing to resubmit."}
    item = run.schedule(
        case_id, "claim", int(option["lands"]), option["result"],
        option.get("sets"), option.get("schedules"),
    )
    return {"accepted": True, "pending_id": item.id, "expected_day": item.due_day}


@app.post("/calls", tags=["channels"])
def place_call(body: CallRequest, x_run_id: Optional[str] = Header(default=None)) -> dict:
    _require_case(body.case_id)
    run = run_of(x_run_id)
    _require_open(run, body.case_id)
    entry = world.match(body.case_id, "calls", body.to, run.known(body.case_id), day=run.day)
    run.log(body.case_id, "call", {"to": body.to, "purpose": body.purpose,
                                   "outcome": (entry or {}).get("outcome", "no_answer")})
    if not entry:
        return {"outcome": "no_answer", "transcript": None,
                "detail": "Rang out. Nobody picked up."}
    run.learn(body.case_id, entry.get("sets"))
    for follow in entry.get("schedules") or []:
        run.schedule(body.case_id, follow.get("kind", "update"), int(follow.get("delay", 0)),
                     follow.get("result", ""), follow.get("sets"), follow.get("schedules"))
    return {"outcome": entry.get("outcome", "connected"),
            "party": entry.get("party"),
            "transcript": entry.get("transcript")}


@app.post("/faxes", tags=["channels"])
def send_fax(body: FaxRequest, x_run_id: Optional[str] = Header(default=None)) -> dict:
    entry_case = _require_case(body.case_id)
    run = run_of(x_run_id)
    _require_open(run, body.case_id)
    known_docs = set((entry_case.get("documents") or {}).keys())
    unknown = [d for d in body.documents if d not in known_docs]
    if unknown:
        raise HTTPException(400, f"no such document(s) on {body.case_id}: {', '.join(unknown)}")
    entry = world.match(body.case_id, "faxes", body.to, run.known(body.case_id), body.documents, day=run.day)
    run.log(body.case_id, "fax", {"to": body.to, "documents": body.documents})
    if entry and entry.get("lands") != world.NEVER:
        run.schedule(body.case_id, "fax", int(entry["lands"]), entry.get("result", ""),
                     entry.get("sets"), entry.get("schedules"))
    # A fax machine doesn't tell you whether anyone works the queue on the other
    # end. Replies, if any, land in the inbox.
    return {"sent": True, "detail": "Transmission reported as delivered."}


@app.post("/texts", tags=["channels"])
def send_text(body: TextRequest, x_run_id: Optional[str] = Header(default=None)) -> dict:
    _require_case(body.case_id)
    run = run_of(x_run_id)
    _require_open(run, body.case_id)
    entry = world.match(body.case_id, "texts", "", run.known(body.case_id), day=run.day)
    run.log(body.case_id, "text", {"body": body.body})
    if not entry or entry.get("lands") == world.NEVER:
        # Sent is all you know. Whether a reply ever comes is not knowable here.
        return {"sent": True}
    item = run.schedule(body.case_id, "text", int(entry["lands"]), entry.get("reply", ""),
                        entry.get("sets"), entry.get("schedules"))
    if item.due_day <= run.day:
        run.deliver_due()
        return {"sent": True, "reply": entry.get("reply")}
    return {"sent": True}


@app.get("/clock", tags=["clock"])
def get_clock(x_run_id: Optional[str] = Header(default=None)) -> dict:
    run = run_of(x_run_id)
    # Only claims you filed are visible in flight. Whether a fax or a text will
    # ever be answered is exactly what this world doesn't tell you.
    return {"day": run.day,
            "pending": [{"id": p.id, "case_id": p.case_id, "kind": p.kind, "due_day": p.due_day}
                        for p in run.pending if not p.delivered and p.kind == "claim"]}


@app.post("/clock/advance", tags=["clock"])
def advance_clock(body: AdvanceRequest, x_run_id: Optional[str] = Header(default=None)) -> dict:
    run = run_of(x_run_id)
    run.day += body.days
    return {"day": run.day, "delivered": run.deliver_due()}


@app.get("/inbox", tags=["clock"])
def get_inbox(case_id: Optional[str] = None, x_run_id: Optional[str] = Header(default=None)) -> list[dict]:
    run = run_of(x_run_id)
    return [i for i in run.inbox if case_id is None or i["case_id"] == case_id]


@app.post("/cases/{case_id}/resolve", tags=["endings"])
def resolve(case_id: str, body: ResolveRequest,
            x_run_id: Optional[str] = Header(default=None)) -> dict:
    _require_case(case_id)
    run = run_of(x_run_id)
    _require_open(run, case_id)
    run.endings[case_id] = Ending(case_id, "resolved", body.model_dump(), run.day)
    run.log(case_id, "resolve", {"summary": body.summary})
    return {"case_id": case_id, "status": "resolved", "day": run.day}


@app.post("/cases/{case_id}/escalate", tags=["endings"])
def escalate(case_id: str, body: EscalateRequest,
             x_run_id: Optional[str] = Header(default=None)) -> dict:
    _require_case(case_id)
    run = run_of(x_run_id)
    _require_open(run, case_id)
    run.endings[case_id] = Ending(case_id, "escalated", body.model_dump(), run.day)
    run.log(case_id, "escalate", {"reason": body.reason})
    return {"case_id": case_id, "status": "escalated", "day": run.day}


@app.get("/run/report", tags=["meta"])
def report(x_run_id: Optional[str] = Header(default=None)) -> dict:
    run = run_of(x_run_id)
    per_case = []
    for case_id in world.case_ids():
        ending = run.endings.get(case_id)
        per_case.append({
            "case_id": case_id,
            "status": ending.outcome if ending else "open",
            "ended_on_day": ending.day if ending else None,
            "detail": ending.detail if ending else None,
            "actions": sum(1 for a in run.actions if a["case_id"] == case_id),
        })
    return {"run_id": run.id, "day": run.day, "cases": per_case,
            "total_actions": len(run.actions), "log": run.actions}


@app.post("/runs/{run_id}/reset", tags=["meta"])
def reset(run_id: str) -> dict:
    RUNS.reset(run_id)
    return {"run_id": run_id, "day": 0, "reset": True}

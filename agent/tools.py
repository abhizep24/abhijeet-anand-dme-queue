"""Tool surface exposed to the LLM.

Every tool argument is validated against a Pydantic model *before* it reaches the
HTTP layer. The JSON schemas handed to the model are generated from the
same models, so the contract can't drift.

`case_id` is deliberately NOT a model-supplied field on case-scoped tools: the
runtime injects the current case id. That keeps the one-agent rule honest (the
agent never chooses which case it's working) and makes cross-case leakage
impossible.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# --- argument models --------------------------------------------------------
class GetCaseArgs(BaseModel):
    pass


class ReadDocumentArgs(BaseModel):
    ref: str = Field(description="A document ref listed on the case (e.g. 'written-order').")


class ListPolicyArgs(BaseModel):
    pass


class ReadPolicyArgs(BaseModel):
    name: str = Field(description="A policy doc name from list_policy (e.g. 'manual-wheelchairs').")


class GetDirectoryArgs(BaseModel):
    pass


class GetClaimArgs(BaseModel):
    claim_id: str


class ResubmitClaimArgs(BaseModel):
    claim_id: str


class PlaceCallArgs(BaseModel):
    to: str = Field(description="Phone number or the name of the party to call.")
    purpose: Optional[str] = Field(default=None, description="Why you're calling. Recorded, not interpreted.")


class SendFaxArgs(BaseModel):
    to: str = Field(description="Fax number or party name.")
    documents: list[str] = Field(default_factory=list, description="Document refs on this case to transmit.")
    note: Optional[str] = None


class SendTextArgs(BaseModel):
    body: str = Field(description="Message to the patient.")


class CheckInboxArgs(BaseModel):
    pass


class AdvanceTimeArgs(BaseModel):
    reason: str = Field(
        description="Why nothing is actionable yet and you need to wait for a pending "
        "async reply (fax/text/claim). The runtime decides how far to advance."
    )


class ResolveArgs(BaseModel):
    summary: str = Field(description="What the blocker was, what removed it, and how you know.")
    evidence: list[str] = Field(
        description="evidence_ids you observed earlier that prove the blocker is gone. "
        "Must be ids the runtime returned to you — fabricated ids are rejected."
    )
    rationale: str = Field(
        description="One or two sentences arguing why this evidence supports RESOLVE "
        "specifically, rather than escalate."
    )


class EscalateArgs(BaseModel):
    reason: str = Field(description="The one-line reason a human is needed.")
    package: str = Field(
        description="Full handoff: current blocker, evidence, actions tried and their "
        "outcomes, hypotheses considered, remaining uncertainty, recommended next step."
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="evidence_ids you observed that support this escalation (may be empty "
        "if the blocker is precisely that evidence could not be obtained).",
    )
    rationale: str = Field(
        description="One or two sentences on why this cannot be safely resolved and must go to a human."
    )


# --- tool registry ----------------------------------------------------------
# name -> (arg model, human description)
TOOL_MODELS: dict[str, type[BaseModel]] = {
    "get_case": GetCaseArgs,
    "read_document": ReadDocumentArgs,
    "list_policy": ListPolicyArgs,
    "read_policy": ReadPolicyArgs,
    "get_directory": GetDirectoryArgs,
    "get_claim": GetClaimArgs,
    "resubmit_claim": ResubmitClaimArgs,
    "place_call": PlaceCallArgs,
    "send_fax": SendFaxArgs,
    "send_text": SendTextArgs,
    "check_inbox": CheckInboxArgs,
    "advance_time": AdvanceTimeArgs,
    "resolve_case": ResolveArgs,
    "escalate_case": EscalateArgs,
}

_DESCRIPTIONS: dict[str, str] = {
    "get_case": "Read the case file: the advocate's summary (a lead, not ground truth), "
                "the list of attached document refs, and the claim ids. Start here.",
    "read_document": "Read the full text of one attached document by its ref.",
    "list_policy": "List the published coverage policy documents available.",
    "read_policy": "Read one coverage policy document in full.",
    "get_directory": "Read the hand-maintained directory of suppliers, practices, payers "
                     "and their phone/fax numbers. Numbers may be stale.",
    "get_claim": "Read the current remittance / status text for a claim id.",
    "resubmit_claim": "Re-processes a claim with its ORIGINAL billing details exactly as first "
                      "filed — it does not apply anything you learned. It pays only if the "
                      "underlying problem was already corrected; otherwise it earns the same "
                      "denial. In particular you CANNOT fix a claim billed to the wrong payer "
                      "(or missing a modifier/code) with this tool: the supplier is the party "
                      "that bills, so you must first reach the supplier, give them the corrected "
                      "information, and have them re-bill. Only then does resubmitting pay.",
    "place_call": "Place a phone call. Returns an outcome and a transcript. A 200 only means "
                  "the call connected to *something* — read the transcript to learn whether "
                  "you reached the right party.",
    "send_fax": "Fax documents to a number. Delivery of the transmission says nothing about "
                "whether anyone works that queue; any reply lands later in the inbox.",
    "send_text": "Text the patient. Replies, if any, arrive later in the inbox.",
    "check_inbox": "Read everything that has landed in the inbox for this case so far.",
    "advance_time": "Advance the simulated clock to wait for pending async replies. You do "
                    "NOT choose the number of days — the runtime advances to the next "
                    "relevant event and returns whatever newly landed.",
    "resolve_case": "END the case as RESOLVED. Only allowed with evidence you actually "
                    "fetched showing the blocker is gone. A false resolve is the worst "
                    "possible outcome.",
    "escalate_case": "END the case as ESCALATED to a human. A valid, successful outcome when "
                     "the blocker can't be safely resolved. Escalate rather than guess.",
}


def _schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in (schema.get("properties") or {}).values():
        prop.pop("title", None)
    schema.setdefault("properties", {})
    schema["additionalProperties"] = False
    return schema


def anthropic_tools() -> list[dict]:
    """The tools array passed to the Anthropic Messages API."""
    return [
        {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "input_schema": _schema(model),
        }
        for name, model in TOOL_MODELS.items()
    ]


TERMINAL_TOOLS = {"resolve_case", "escalate_case"}

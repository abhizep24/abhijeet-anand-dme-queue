"""Thin async HTTP wrapper around the mock world.

This is the *only* place that talks to the world over HTTP. It does no
interpretation — it returns either a parsed JSON body or a structured error dict
so the runtime above can turn any failure into an observation instead of a crash.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import WORLD_BASE_URL


class WorldClient:
    def __init__(self, run_id: str, base_url: str = WORLD_BASE_URL, timeout: float = 30.0):
        self.run_id = run_id
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Run-Id": run_id},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Return {'ok': True, 'data': ...} or {'ok': False, 'error': ...}.

        Never raises for HTTP/transport errors — the runtime treats failures as
        observations the agent can reason about.
        """
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"transport error contacting world: {exc}"}
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            return {"ok": False, "status": resp.status_code, "error": detail}
        try:
            return {"ok": True, "data": resp.json()}
        except Exception:
            return {"ok": True, "data": resp.text}

    # --- reads -------------------------------------------------------------
    async def list_cases(self):
        return await self._request("GET", "/cases")

    async def get_case(self, case_id: str):
        return await self._request("GET", f"/cases/{case_id}")

    async def get_document(self, case_id: str, ref: str):
        return await self._request("GET", f"/cases/{case_id}/documents/{ref}")

    async def get_directory(self):
        return await self._request("GET", "/directory")

    async def list_policy(self):
        return await self._request("GET", "/policy")

    async def get_policy(self, name: str):
        return await self._request("GET", f"/policy/{name}")

    async def get_claim(self, claim_id: str):
        return await self._request("GET", f"/claims/{claim_id}")

    async def get_clock(self):
        return await self._request("GET", "/clock")

    async def get_inbox(self, case_id: Optional[str] = None):
        params = {"case_id": case_id} if case_id else None
        return await self._request("GET", "/inbox", params=params)

    # --- actions -----------------------------------------------------------
    async def resubmit_claim(self, claim_id: str):
        return await self._request("POST", f"/claims/{claim_id}/resubmit")

    async def place_call(self, case_id: str, to: str, purpose: Optional[str]):
        return await self._request(
            "POST", "/calls",
            json={"case_id": case_id, "to": to, "purpose": purpose},
        )

    async def send_fax(self, case_id: str, to: str, documents: list[str], note: Optional[str]):
        return await self._request(
            "POST", "/faxes",
            json={"case_id": case_id, "to": to, "documents": documents, "note": note},
        )

    async def send_text(self, case_id: str, body: str):
        return await self._request(
            "POST", "/texts",
            json={"case_id": case_id, "body": body},
        )

    async def advance_clock(self, days: int):
        return await self._request("POST", "/clock/advance", json={"days": days})

    async def resolve(self, case_id: str, summary: str, evidence: list[str]):
        return await self._request(
            "POST", f"/cases/{case_id}/resolve",
            json={"summary": summary, "evidence": evidence},
        )

    async def escalate(self, case_id: str, reason: str, package: str):
        return await self._request(
            "POST", f"/cases/{case_id}/escalate",
            json={"reason": reason, "package": package},
        )

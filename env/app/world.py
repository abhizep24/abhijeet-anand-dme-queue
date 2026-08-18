"""Loads the world from data/ and matches actions against it."""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parents[2] / "data"

NEVER = "never"


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


@lru_cache(maxsize=1)
def world() -> dict:
    return yaml.safe_load((DATA / "world.yaml").read_text())["cases"]


@lru_cache(maxsize=1)
def case_ids() -> tuple[str, ...]:
    return tuple(sorted(world().keys()))


def case(case_id: str) -> dict | None:
    return world().get(case_id)


@lru_cache(maxsize=16)
def case_file(case_id: str) -> str:
    path = DATA / "cases" / f"{case_id}.md"
    return path.read_text() if path.exists() else ""


@lru_cache(maxsize=1)
def directory() -> list[dict]:
    with (DATA / "directory.csv").open() as handle:
        return list(csv.DictReader(handle))


@lru_cache(maxsize=1)
def policy_docs() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in (DATA / "policy").glob("*.md")))


def policy(name: str) -> str | None:
    path = DATA / "policy" / f"{name}.md"
    return path.read_text() if path.exists() else None


def _target_matches(entry: dict, target: str) -> bool:
    """A target matches on phone number, or on the party name."""
    wanted = _digits(target)
    if wanted and wanted == _digits(entry.get("to", "")):
        return True
    party = (entry.get("party") or "").lower()
    probe = (target or "").lower().strip()
    return bool(probe) and bool(party) and (probe in party or party in probe)


def match(
    case_id: str,
    channel: str,
    target: str,
    facts: set[str],
    documents: list[str] | None = None,
    day: int = 0,
) -> dict | None:
    """First entry whose target, facts, documents, and day window all line up."""
    entries = (case(case_id) or {}).get(channel) or []
    documents = documents or []
    for entry in entries:
        if channel != "texts" and not _target_matches(entry, target):
            continue
        if not set(entry.get("requires") or []).issubset(facts):
            continue
        if "after_day" in entry and day < int(entry["after_day"]):
            continue
        if "before_day" in entry and day >= int(entry["before_day"]):
            continue
        required_docs = entry.get("documents")
        if required_docs and not set(required_docs).issubset(set(documents)):
            continue
        return entry
    return None


def claim(case_id: str, claim_id: str) -> dict | None:
    return ((case(case_id) or {}).get("claims") or {}).get(claim_id)


def find_claim(claim_id: str) -> tuple[str, dict] | None:
    for cid in case_ids():
        found = claim(cid, claim_id)
        if found:
            return cid, found
    return None


def resubmission(case_id: str, claim_id: str, facts: set[str]) -> dict | None:
    entry = claim(case_id, claim_id)
    if not entry:
        return None
    for option in entry.get("resubmit") or []:
        if set(option.get("requires") or []).issubset(facts):
            return option
    return None

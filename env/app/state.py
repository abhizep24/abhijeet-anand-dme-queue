"""Per-run state: the clock, what each case knows, what's in flight."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class Pending:
    id: int
    case_id: str
    kind: str
    due_day: int
    body: str
    sets: list[str] = field(default_factory=list)
    schedules: list[dict] = field(default_factory=list)
    delivered: bool = False


@dataclass
class Ending:
    case_id: str
    outcome: str
    detail: dict
    day: int


@dataclass
class Run:
    id: str
    day: int = 0
    facts: dict[str, set[str]] = field(default_factory=dict)
    pending: list[Pending] = field(default_factory=list)
    inbox: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    endings: dict[str, Ending] = field(default_factory=dict)

    def known(self, case_id: str) -> set[str]:
        return self.facts.setdefault(case_id, set())

    def learn(self, case_id: str, facts: list[str] | None) -> None:
        if facts:
            self.known(case_id).update(facts)

    def log(self, case_id: str, channel: str, detail: dict) -> None:
        self.actions.append(
            {"n": len(self.actions) + 1, "day": self.day, "case_id": case_id,
             "channel": channel, **detail}
        )

    def schedule(self, case_id: str, kind: str, delay: int, body: str,
                 sets: list[str] | None = None,
                 schedules: list[dict] | None = None) -> Pending:
        item = Pending(
            id=next(_ids), case_id=case_id, kind=kind, due_day=self.day + delay,
            body=body, sets=list(sets or []), schedules=list(schedules or []),
        )
        self.pending.append(item)
        return item

    def deliver_due(self) -> list[dict]:
        """Deliver everything now due, including anything it triggers."""
        delivered: list[dict] = []
        while True:
            due = [p for p in self.pending if not p.delivered and p.due_day <= self.day]
            if not due:
                return delivered
            for item in due:
                item.delivered = True
                self.learn(item.case_id, item.sets)
                record = {
                    "id": item.id, "case_id": item.case_id, "kind": item.kind,
                    "day": item.due_day, "body": item.body,
                }
                self.inbox.append(record)
                delivered.append(record)
                for follow in item.schedules:
                    child = self.schedule(
                        item.case_id, follow.get("kind", item.kind),
                        int(follow.get("delay", 0)), follow.get("result", ""),
                        follow.get("sets"), follow.get("schedules"),
                    )
                    child.due_day = item.due_day + int(follow.get("delay", 0))


class Runs:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def get(self, run_id: str) -> Run:
        return self._runs.setdefault(run_id, Run(id=run_id))

    def reset(self, run_id: str) -> Run:
        self._runs[run_id] = Run(id=run_id)
        return self._runs[run_id]


RUNS = Runs()

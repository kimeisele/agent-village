"""
Agent Village — Work Result
============================
Neutral, JSON-native schema for the output of a single worker execution
against a VillageContract. No provider-specific fields outside neutral
`provider`/`model` metadata strings (SPEC.md §A.6).

A WorkResult is NOT a Contract fulfillment. See village/worker.py: the
worker that produces a WorkResult never calls
`VillageContract.fulfill()`/`village.heartbeat.bounty_complete()`. A
WorkResult is submitted, review-pending evidence -- the model's own
output is never treated as an authoritative claim of success (SPEC.md
§A.5: no LLM output ever gets direct write authority).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from village.contracts import normalize_datetime

SCHEMA_VERSION = "1"


class WorkResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_OUTPUT = "invalid_output"
    PROVIDER_ERROR = "provider_error"


@dataclass
class WorkResult:
    work_result_id: str
    contract_id: str
    execution_id: str
    provider: str
    model: str
    status: WorkResultStatus
    output: dict[str, Any] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    error: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.started_at = normalize_datetime(self.started_at)
        self.finished_at = normalize_datetime(self.finished_at)
        if isinstance(self.status, str):
            self.status = WorkResultStatus(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_result_id": self.work_result_id,
            "contract_id": self.contract_id,
            "execution_id": self.execution_id,
            "provider": self.provider,
            "model": self.model,
            "status": self.status.value,
            "output": self.output,
            "evidence": self.evidence,
            "usage": self.usage,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkResult":
        started_at = d.get("started_at")
        finished_at = d.get("finished_at")
        return cls(
            work_result_id=d["work_result_id"],
            contract_id=d["contract_id"],
            execution_id=d["execution_id"],
            provider=d["provider"],
            model=d["model"],
            status=WorkResultStatus(d["status"]),
            output=d.get("output"),
            evidence=d.get("evidence", {}),
            usage=d.get("usage", {}),
            started_at=datetime.fromisoformat(started_at) if started_at else datetime.now(timezone.utc),
            finished_at=datetime.fromisoformat(finished_at) if finished_at else None,
            error=d.get("error"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "WorkResult":
        return cls.from_dict(json.loads(s))

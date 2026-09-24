"""Safe, ordered event records and their canonical JSONL representation."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from .contracts import ApprovalDecision, ApprovalType, Identifier, RunState, StrictModel


class EventType(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    PLAN_REPLACED = "PLAN_REPLACED"
    STATE_TRANSITION = "STATE_TRANSITION"
    APPROVAL_RECORDED = "APPROVAL_RECORDED"
    RESULT_IDENTIFIED = "RESULT_IDENTIFIED"


class Event(StrictModel):
    run_id: Identifier
    sequence: int = Field(gt=0)
    timestamp: datetime
    event_type: EventType
    payload: dict[str, str | int]

    _fields: ClassVar[dict[EventType, frozenset[str]]] = {
        EventType.RUN_CREATED: frozenset({"plan_fingerprint", "plan_revision", "state"}),
        EventType.PLAN_REPLACED: frozenset(
            {"plan_fingerprint", "plan_revision", "from_state", "to_state"}
        ),
        EventType.STATE_TRANSITION: frozenset({"plan_revision", "from_state", "to_state"}),
        EventType.APPROVAL_RECORDED: frozenset(
            {"approval_type", "decision", "subject_fingerprint", "plan_revision"}
        ),
        EventType.RESULT_IDENTIFIED: frozenset({"result_fingerprint"}),
    }

    @field_validator("timestamp")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def safe_payload(self) -> Event:
        if self.payload.keys() != self._fields[self.event_type]:
            raise ValueError("event payload has missing or unsafe fields")
        for key, value in self.payload.items():
            if key == "plan_revision":
                if type(value) is not int or value <= 0:
                    raise ValueError("plan_revision must be a positive integer")
            elif key.endswith("fingerprint"):
                if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise ValueError(f"{key} must be a SHA-256 fingerprint")
            elif key in {"state", "from_state", "to_state"}:
                if type(value) is not str or value not in RunState._value2member_map_:
                    raise ValueError(f"{key} must be a run state")
            elif key == "approval_type":
                if type(value) is not str or value not in ApprovalType._value2member_map_:
                    raise ValueError("approval_type must be recognized")
            elif key == "decision" and (
                type(value) is not str or value not in ApprovalDecision._value2member_map_
            ):
                raise ValueError("decision must be recognized")
        return self


def event_json(event: Event) -> str:
    """Canonical single-line encoding for SQLite and the JSONL mirror."""
    return json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )

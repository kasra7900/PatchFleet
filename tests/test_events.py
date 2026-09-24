"""Event payloads contain only safe control-plane metadata."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from patchfleet.events import Event, EventType


def test_event_rejects_raw_prompt_field() -> None:
    with pytest.raises(ValidationError, match="unsafe fields"):
        Event(
            run_id="run-1",
            sequence=1,
            timestamp=datetime.now(UTC),
            event_type=EventType.RUN_CREATED,
            payload={
                "plan_fingerprint": "a" * 64,
                "plan_revision": 1,
                "state": "DRAFT",
                "raw_prompt": "private user request",
            },
        )

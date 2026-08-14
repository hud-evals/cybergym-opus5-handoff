from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from hud.agents.types import AgentStep
from hud.types import Step

from cybergym_hud.trace_tail import (
    OpenHandsEventTailer,
    OpenHandsTraceError,
)


@dataclass(frozen=True, slots=True)
class FakeProjectedStep:
    key: str
    step: Step


class FakeProjector:
    """Small prefix-stable projector; its decode method deliberately whitelists."""

    def decode(self, payload: object, *, origin: str) -> object | None:
        assert isinstance(payload, Mapping)
        assert origin.startswith("sessions/")
        if payload.get("kind") != "agent":
            return None
        return {
            "key": str(payload["key"]),
            "text": str(payload["text"]),
            "ready": payload.get("ready") is True,
        }

    def project(
        self,
        events: Sequence[object],
        *,
        final: bool,
    ) -> Sequence[FakeProjectedStep]:
        projected: list[FakeProjectedStep] = []
        for item in events:
            assert isinstance(item, dict)
            if item["ready"] is not True and not final:
                continue
            projected.append(
                FakeProjectedStep(
                    key=str(item["key"]),
                    step=AgentStep(content=str(item["text"])),
                )
            )
        return projected


def _event_dir(receipt_dir: Path, session: str = "session") -> Path:
    path = receipt_dir / "file" / "sessions" / session / "events"
    path.mkdir(parents=True)
    return path


def _write_event(path: Path, **payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_live_tail_waits_for_a_stable_file_and_finally_reconciles(tmp_path: Path) -> None:
    emitted = []
    first = threading.Event()
    events_dir = _event_dir(tmp_path)

    def emit(step: AgentStep) -> None:
        emitted.append(step)
        first.set()

    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emit,
        poll_interval=0.02,
    )
    tailer.start()

    event_zero = events_dir / "0.json"
    event_zero.write_text('{"kind":"agent"', encoding="utf-8")
    time.sleep(0.08)
    assert emitted == []

    _write_event(
        event_zero,
        kind="agent",
        key="response:one",
        text="first",
        ready=True,
        screenshot="must never survive decode",
        raw={"provider": "must never survive decode"},
    )
    assert first.wait(2)
    assert [step.content for step in emitted] == ["first"]

    # Stop before this file can pass the two-live-scan stability gate.  The
    # final pass must still reconcile it exactly once.
    _write_event(
        events_dir / "1.json",
        kind="agent",
        key="response:two",
        text="second",
        ready=False,
    )
    tailer.finish()

    assert [step.content for step in emitted] == ["first", "second"]
    assert tailer.emitted_keys == ("response:one", "response:two")
    assert tailer.emitted_step_count == 2
    assert tailer.emitted_step_counts == {"agent": 2}
    assert tailer.final_event_count == 2
    assert tailer.final_step_count == 2


def test_repeated_reconciliation_never_reemits_a_step(tmp_path: Path) -> None:
    emitted = []
    events_dir = _event_dir(tmp_path)
    _write_event(
        events_dir / "0.json",
        kind="agent",
        key="response:one",
        text="first",
        ready=True,
    )
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        poll_interval=0.01,
    )
    tailer.start()
    _wait_until(lambda: len(emitted) == 1)
    time.sleep(0.05)
    tailer.finish()
    assert len(emitted) == 1


def test_live_tail_waits_at_an_id_gap_then_emits_in_numeric_order(tmp_path: Path) -> None:
    emitted = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="zero", ready=True)
    _write_event(events_dir / "2.json", kind="agent", key="two", text="two", ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        poll_interval=0.01,
    )
    tailer.start()
    _wait_until(lambda: len(emitted) == 1)
    time.sleep(0.04)
    assert [step.content for step in emitted] == ["zero"]

    _write_event(events_dir / "1.json", kind="agent", key="one", text="one", ready=True)
    _wait_until(lambda: len(emitted) == 3)
    tailer.finish()

    assert [step.content for step in emitted] == ["zero", "one", "two"]


def test_final_reconciliation_rejects_a_missing_event_id(tmp_path: Path) -> None:
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="zero", ready=True)
    _write_event(events_dir / "2.json", kind="agent", key="two", text="two", ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        poll_interval=0.01,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert tailer.final_event_count is None
    assert tailer.final_step_count is None


def test_final_reconciliation_requires_exactly_one_nonempty_session(tmp_path: Path) -> None:
    for session in ("first", "second"):
        events_dir = _event_dir(tmp_path, session)
        _write_event(events_dir / "0.json", kind="agent", key=session, text=session, ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        poll_interval=0.01,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()


def test_final_reconciliation_rejects_an_empty_event_store(tmp_path: Path) -> None:
    _event_dir(tmp_path)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        poll_interval=0.01,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()


def test_append_only_rewrite_fails_closed(tmp_path: Path) -> None:
    emitted = []
    events_dir = _event_dir(tmp_path)
    event = events_dir / "0.json"
    _write_event(event, kind="agent", key="response:one", text="first", ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        poll_interval=0.01,
    )
    tailer.start()
    _wait_until(lambda: len(emitted) == 1)
    _write_event(event, kind="agent", key="response:one", text="changed", ready=True)
    time.sleep(0.04)

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()


class DuplicateProjector(FakeProjector):
    def project(
        self,
        events: Sequence[object],
        *,
        final: bool,
    ) -> Sequence[FakeProjectedStep]:
        step = FakeProjectedStep(key="duplicate", step=AgentStep(content="same"))
        return [step, step] if events else []


def test_duplicate_semantic_keys_fail_closed(tmp_path: Path) -> None:
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="ignored", text="text", ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=DuplicateProjector(),
        sink=lambda _step: None,
        poll_interval=0.01,
    )
    tailer.start()
    time.sleep(0.04)

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()


def test_incomplete_final_event_fails_closed(tmp_path: Path) -> None:
    events_dir = _event_dir(tmp_path)
    (events_dir / "0.json").write_text('{"kind":', encoding="utf-8")
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        poll_interval=0.01,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_symlinked_event_file_fails_closed(tmp_path: Path) -> None:
    events_dir = _event_dir(tmp_path)
    target = tmp_path / "outside.json"
    _write_event(target, kind="agent", key="response:one", text="secret", ready=True)
    os.symlink(target, events_dir / "0.json")
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        poll_interval=0.01,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()

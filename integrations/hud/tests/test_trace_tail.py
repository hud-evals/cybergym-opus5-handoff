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
    SavedTrajectoryProjection,
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
        **_kwargs: object,
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


def _saved_projection(
    *items: tuple[str, str],
    source_event_ids: frozenset[str] = frozenset(),
) -> SavedTrajectoryProjection:
    return SavedTrajectoryProjection(
        steps=tuple(FakeProjectedStep(key=key, step=AgentStep(content=text)) for key, text in items),
        source_event_ids=source_event_ids,
    )


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
    assert [item.key for item in tailer.projection_snapshot()] == [
        "response:one",
        "response:two",
    ]


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


def test_oversized_event_is_never_read_and_saved_projection_supplies_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    emitted = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="x" * 100, ready=True)
    _write_event(events_dir / "1.json", kind="agent", key="one", text="suffix", ready=True)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("oversized raw event or its suffix was read")

    monkeypatch.setattr("cybergym_hud.trace_tail.os.read", forbidden_read)
    saved = _saved_projection(
        ("zero", "sanitized zero"),
        ("one", "sanitized one"),
        source_event_ids=frozenset({"0", "1"}),
    )
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        saved_projection_loader=lambda _final: saved,
        poll_interval=0.01,
        max_event_bytes=64,
    )
    tailer.start()
    tailer.finish()

    assert [step.content for step in emitted] == ["sanitized zero", "sanitized one"]
    assert tailer.final_event_count == 2
    assert tailer.final_step_count == 2
    assert [item.key for item in tailer.projection_snapshot()] == ["zero", "one"]


@pytest.mark.parametrize("final_projection", [True, False])
def test_oversized_fallback_preserves_live_prefix_and_emits_only_missing_suffix(
    tmp_path: Path,
    final_projection: bool,
) -> None:
    emitted = []
    loader_calls: list[bool] = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="live zero", ready=True)
    _write_event(events_dir / "1.json", kind="agent", key="one", text="x" * 300, ready=True)
    _write_event(events_dir / "2.json", kind="agent", key="two", text="must not be read live", ready=True)
    saved = _saved_projection(
        ("zero", "live zero"),
        ("one", "sanitized one"),
        ("two", "sanitized two"),
        source_event_ids=frozenset({"0", "1", "2"}),
    )

    def load(final: bool) -> SavedTrajectoryProjection:
        loader_calls.append(final)
        return saved

    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        saved_projection_loader=load,
        poll_interval=0.01,
        max_event_bytes=256,
    )
    tailer.start()
    _wait_until(lambda: len(emitted) == 1)
    assert [step.content for step in emitted] == ["live zero"]

    tailer.finish(final_projection=final_projection)

    assert loader_calls == [final_projection]
    assert [step.content for step in emitted] == ["live zero", "sanitized one", "sanitized two"]
    assert tailer.emitted_keys == ("zero", "one", "two")
    assert [item.key for item in tailer.projection_snapshot()] == ["zero", "one", "two"]


def _oversized_after_live_prefix(
    tmp_path: Path,
    *,
    loader,
) -> tuple[OpenHandsEventTailer, list[AgentStep]]:
    emitted: list[AgentStep] = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="live zero", ready=True)
    _write_event(events_dir / "1.json", kind="agent", key="one", text="x" * 300, ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        saved_projection_loader=loader,
        poll_interval=0.01,
        max_event_bytes=256,
    )
    tailer.start()
    _wait_until(lambda: len(emitted) == 1)
    return tailer, emitted


def test_oversized_event_without_saved_fallback_fails_closed(tmp_path: Path) -> None:
    tailer, emitted = _oversized_after_live_prefix(tmp_path, loader=None)

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert [step.content for step in emitted] == ["live zero"]


@pytest.mark.parametrize(
    "loader",
    [
        lambda _final: (_ for _ in ()).throw(FileNotFoundError("missing trajectory")),
        lambda _final: object(),
        lambda _final: _saved_projection(
            ("zero", "live zero"),
            ("one", "sanitized one"),
            source_event_ids=frozenset({"0"}),
        ),
        lambda _final: _saved_projection(source_event_ids=frozenset({"1"})),
        lambda _final: _saved_projection(
            ("divergent", "live zero"),
            ("one", "sanitized one"),
            source_event_ids=frozenset({"1"}),
        ),
        lambda _final: _saved_projection(
            ("zero", "changed zero"),
            ("one", "sanitized one"),
            source_event_ids=frozenset({"1"}),
        ),
        lambda _final: SavedTrajectoryProjection(
            steps=(object(),),
            source_event_ids=frozenset({"1"}),
        ),
    ],
    ids=[
        "missing",
        "malformed-wrapper",
        "missing-oversized-id",
        "shorter",
        "divergent-key",
        "divergent-payload",
        "malformed-step",
    ],
)
def test_invalid_saved_fallbacks_fail_closed(tmp_path: Path, loader) -> None:
    tailer, emitted = _oversized_after_live_prefix(tmp_path, loader=loader)

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert [step.content for step in emitted] == ["live zero"]


@pytest.mark.parametrize("mutation", ["shrink", "disappear", "move-boundary"])
def test_deferred_oversized_event_cannot_change_before_finalization(tmp_path: Path, mutation: str) -> None:
    loader_calls = []
    events_dir = _event_dir(tmp_path)
    oversized = events_dir / "0.json"
    _write_event(oversized, kind="agent", key="zero", text="x" * 100, ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        saved_projection_loader=lambda final: (
            loader_calls.append(final) or _saved_projection(("zero", "sanitized"), source_event_ids=frozenset({"0"}))
        ),
        poll_interval=0.01,
        max_event_bytes=64,
    )
    tailer.start()
    _wait_until(lambda: tailer._deferred_oversized_origin is not None)
    if mutation == "shrink":
        _write_event(oversized, kind="ignored")
    else:
        oversized.unlink()
        if mutation == "move-boundary":
            _write_event(events_dir / "1.json", kind="agent", key="one", text="x" * 100, ready=True)

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert loader_calls == []


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_deferred_suffix_symlink_fails_before_saved_fallback(tmp_path: Path) -> None:
    loader_calls = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="x" * 100, ready=True)
    target = tmp_path / "outside.json"
    _write_event(target, kind="agent", key="one", text="outside", ready=True)
    os.symlink(target, events_dir / "1.json")
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        saved_projection_loader=lambda final: (
            loader_calls.append(final) or _saved_projection(("zero", "sanitized"), source_event_ids=frozenset({"0"}))
        ),
        poll_interval=0.01,
        max_event_bytes=64,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert loader_calls == []


def test_deferred_suffix_id_gap_fails_before_saved_fallback(tmp_path: Path) -> None:
    loader_calls = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="x" * 100, ready=True)
    _write_event(events_dir / "2.json", kind="agent", key="two", text="suffix", ready=True)
    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=lambda _step: None,
        saved_projection_loader=lambda final: (
            loader_calls.append(final) or _saved_projection(("zero", "sanitized"), source_event_ids=frozenset({"0"}))
        ),
        poll_interval=0.01,
        max_event_bytes=64,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert loader_calls == []


def test_deferred_suffix_change_during_saved_load_fails_before_emission(tmp_path: Path) -> None:
    emitted = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="x" * 100, ready=True)
    suffix = events_dir / "1.json"
    _write_event(suffix, kind="agent", key="one", text="suffix", ready=True)

    def load(_final: bool) -> SavedTrajectoryProjection:
        _write_event(suffix, kind="agent", key="one", text="changed suffix", ready=True)
        return _saved_projection(
            ("zero", "sanitized zero"),
            ("one", "sanitized one"),
            source_event_ids=frozenset({"0", "1"}),
        )

    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        saved_projection_loader=load,
        poll_interval=0.01,
        max_event_bytes=64,
    )
    tailer.start()

    with pytest.raises(OpenHandsTraceError, match="projection failed"):
        tailer.finish()
    assert emitted == []


def test_saved_fallback_timeout_cancels_late_step_delivery(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    emitted = []
    events_dir = _event_dir(tmp_path)
    _write_event(events_dir / "0.json", kind="agent", key="zero", text="x" * 100, ready=True)

    def load(_final: bool) -> SavedTrajectoryProjection:
        entered.set()
        assert release.wait(timeout=2)
        return _saved_projection(("zero", "sanitized"), source_event_ids=frozenset({"0"}))

    tailer = OpenHandsEventTailer(
        tmp_path,
        projector=FakeProjector(),
        sink=emitted.append,
        saved_projection_loader=load,
        poll_interval=0.01,
        max_event_bytes=64,
    )
    tailer.start()
    assert not entered.wait(timeout=0.05)

    with pytest.raises(OpenHandsTraceError, match="did not stop"):
        tailer.finish(timeout=0.02)
    assert entered.wait(timeout=2)
    release.set()
    assert tailer._thread is not None
    tailer._thread.join(timeout=2)
    assert not tailer._thread.is_alive()
    assert emitted == []


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
        **_kwargs: object,
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

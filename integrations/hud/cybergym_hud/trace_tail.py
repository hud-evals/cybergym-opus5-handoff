"""Live projection of append-only OpenHands events into HUD steps.

The pinned OpenHands controller writes one JSON file per event.  This module
tails that store without importing OpenHands or exposing its raw event shape to
HUD.  A projector owns the strict schema translation; the tailer owns file
safety, ordering, live delivery, and final reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hud.types import Step

_EVENT_FILE = re.compile(r"^(?P<event_id>[0-9]+)\.json$")
_DEFAULT_MAX_EVENT_BYTES = 16 * 1024 * 1024


class OpenHandsTraceError(RuntimeError):
    """The local OpenHands event stream could not be projected safely."""


@dataclass(frozen=True, slots=True)
class SavedTrajectoryProjection:
    """Strict saved projection plus its sanitized source-event identities."""

    steps: tuple[object, ...]
    source_event_ids: frozenset[str]


@dataclass(frozen=True, order=True, slots=True)
class _EventOrigin:
    """Stable identity of one append-only event file."""

    session: str
    event_id: int


class OpenHandsEventProjector(Protocol):
    """Strict translation boundary implemented by the trajectory converter.

    ``decode`` must whitelist fields.  In particular, it must never retain
    screenshots, DOM/a11y trees, raw provider responses, or unrecognized
    ``extras``.  ``project`` returns the complete prefix-stable projection of
    all decoded events supplied so far.  With ``final=False`` it must withhold
    incomplete response groups; ``final=True`` may add only a final suffix.
    """

    def decode(self, payload: object, *, origin: str) -> object | None:
        """Return a sanitized typed event, or ``None`` for an ignored event."""

    def project(
        self,
        events: Sequence[object],
        *,
        final: bool,
        **kwargs: object,
    ) -> Sequence[object]:
        """Return the complete canonical projection for ``events``."""


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    event: object | None
    digest: str


class OpenHandsEventTailer:
    """Tail one OpenHands receipt directory and emit each HUD step once.

    OpenHands writes event files with ordinary ``open(..., "w")`` rather than
    atomic rename, so live polling waits for the same byte digest on two
    consecutive scans before decoding.  After the worker exits, the tailer
    performs an immediate full reconciliation and fails closed on a partial or
    malformed file.
    """

    def __init__(
        self,
        receipt_log_dir: Path,
        *,
        projector: OpenHandsEventProjector,
        sink: Callable[[Step], None],
        saved_projection_loader: Callable[[bool], SavedTrajectoryProjection] | None = None,
        poll_interval: float = 0.25,
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
        project_kwargs: Mapping[str, object] | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("OpenHands trace poll interval must be positive")
        if max_event_bytes <= 0:
            raise ValueError("OpenHands trace event size limit must be positive")
        self._receipt_log_dir = receipt_log_dir
        self._projector = projector
        self._sink = sink
        self._saved_projection_loader = saved_projection_loader
        self._poll_interval = poll_interval
        self._max_event_bytes = max_event_bytes
        self._project_kwargs = dict(project_kwargs or {})
        self._stop = threading.Event()
        self._delivery_cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._events: dict[_EventOrigin, _StoredEvent] = {}
        self._candidates: dict[_EventOrigin, str] = {}
        self._deferred_oversized_origin: _EventOrigin | None = None
        self._emitted_keys: list[str] = []
        self._emitted_payloads: dict[str, str] = {}
        self._emitted_step_counts: Counter[str] = Counter()
        self._final_event_count: int | None = None
        self._final_step_count: int | None = None
        self._final_projection_steps: tuple[object, ...] | None = None
        self._final_projection = True

    def start(self) -> None:
        """Start the single background poller."""

        if self._thread is not None:
            raise RuntimeError("OpenHands event tailer was already started")
        self._thread = threading.Thread(
            target=self._run,
            name="cybergym-openhands-trace-tail",
            daemon=True,
        )
        self._thread.start()

    def finish(self, *, timeout: float = 10.0, final_projection: bool = True) -> None:
        """Stop live polling, reconcile the final store, and surface failures."""

        thread = self._thread
        if thread is None:
            raise RuntimeError("OpenHands event tailer was not started")
        self._final_projection = final_projection
        self._stop.set()
        thread.join(timeout)
        if thread.is_alive():
            # A saved-trajectory loader is ordinary synchronous filesystem
            # code and cannot be killed safely.  Prevent its eventual return
            # from delivering steps into a trace whose owner has timed out.
            self._delivery_cancelled.set()
            raise OpenHandsTraceError("OpenHands event tailer did not stop")
        if self._error is not None:
            raise OpenHandsTraceError("OpenHands event projection failed") from self._error

    @property
    def emitted_keys(self) -> tuple[str, ...]:
        """Stable identities emitted so far, primarily for diagnostics/tests."""

        return tuple(self._emitted_keys)

    @property
    def emitted_step_count(self) -> int:
        """Number of canonical HUD steps successfully delivered so far."""

        return len(self._emitted_keys)

    @property
    def emitted_step_counts(self) -> dict[str, int]:
        """Delivered step counts grouped by HUD source (agent/tool/etc.)."""

        return dict(self._emitted_step_counts)

    @property
    def final_event_count(self) -> int | None:
        """Number of contiguous OpenHands files after successful finalization."""

        return self._final_event_count

    @property
    def final_step_count(self) -> int | None:
        """Number of canonical projected steps after successful finalization."""

        return self._final_step_count

    def projection_snapshot(self) -> tuple[object, ...]:
        """Return the finalized canonical projection without reading raw files."""

        if self._final_projection_steps is None:
            raise OpenHandsTraceError("OpenHands event tailer has not finalized")
        return self._final_projection_steps

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._reconcile(final=False)
                self._stop.wait(self._poll_interval)
            self._reconcile(final=True, projection_final=self._final_projection)
        except BaseException as exc:
            self._error = exc

    def _reconcile(self, *, final: bool, projection_final: bool | None = None) -> None:
        paths = self._event_paths(final=final)
        oversized_origin: _EventOrigin | None = None
        for origin, path in paths:
            try:
                payload, digest = self._read_event_file(path)
            except _OversizedEventFile as exc:
                if origin in self._events:
                    raise OpenHandsTraceError(
                        f"OpenHands rewrote append-only event {origin.session}/{origin.event_id}"
                    ) from exc
                if self._deferred_oversized_origin not in {None, origin}:
                    raise OpenHandsTraceError("OpenHands oversized event boundary changed") from exc
                self._deferred_oversized_origin = origin
                oversized_origin = origin
                # Do not read or decode this file or any suffix after it.  A
                # final saved-trajectory reconciliation is the only allowed
                # source for the missing semantic suffix.
                break
            except _UnstableEventFile as exc:
                if final:
                    raise OpenHandsTraceError(
                        f"OpenHands event {origin.session}/{origin.event_id} changed during final reconciliation"
                    ) from exc
                self._candidates.pop(origin, None)
                continue
            if origin == self._deferred_oversized_origin:
                raise OpenHandsTraceError("OpenHands deferred oversized event changed before finalization")
            stored = self._events.get(origin)
            if stored is not None:
                if stored.digest != digest:
                    raise OpenHandsTraceError(f"OpenHands rewrote append-only event {origin.session}/{origin.event_id}")
                continue

            if not final and self._candidates.get(origin) != digest:
                self._candidates[origin] = digest
                continue

            decoded = self._decode(payload, origin=origin, final=final)
            if decoded is _RETRY:
                continue
            self._events[origin] = _StoredEvent(
                event=decoded,
                digest=digest,
            )
            self._candidates.pop(origin, None)

        events: list[object] = []
        for origin, _path in paths:
            stored = self._events.get(origin)
            if stored is None:
                # A higher-numbered event can stabilize before an earlier
                # ordinary ``open(..., "w")`` write.  Never expose that
                # out-of-order suffix to the semantic projector.
                break
            if stored.event is not None:
                events.append(stored.event)
        semantic_final = final if projection_final is None else projection_final
        if oversized_origin is not None:
            prefix_projection = list(
                self._projector.project(
                    events,
                    # The oversized event may complete the current provider
                    # response, so the decoded raw prefix is never terminal.
                    final=False,
                    **self._project_kwargs,
                )
            )
            self._emit_new_suffix(prefix_projection)
            if not final:
                return
            deferred_stats = self._deferred_path_stats(paths, oversized_origin)
            projection = self._load_saved_projection(semantic_final, oversized_origin)
            if self._deferred_path_stats(paths, oversized_origin) != deferred_stats:
                raise OpenHandsTraceError("OpenHands deferred event suffix changed during finalization")
            self._assert_projection_prefix(prefix_projection, projection)
        else:
            if final and self._deferred_oversized_origin is not None:
                raise OpenHandsTraceError("OpenHands deferred oversized event disappeared before finalization")
            projection = list(
                self._projector.project(
                    events,
                    final=semantic_final,
                    **self._project_kwargs,
                )
            )
        self._emit_new_suffix(projection)
        if final:
            if len(self._events) != len(paths):
                if oversized_origin is None:
                    raise OpenHandsTraceError("OpenHands final reconciliation omitted an event file")
                expected_prefix = next(
                    index for index, (origin, _path) in enumerate(paths) if origin == oversized_origin
                )
                if len(self._events) != expected_prefix:
                    raise OpenHandsTraceError("OpenHands oversized event prefix was not fully reconciled")
            self._final_event_count = len(paths)
            self._final_step_count = len(projection)
            self._final_projection_steps = tuple(projection)

    def _load_saved_projection(self, final: bool, oversized_origin: _EventOrigin) -> list[object]:
        loader = self._saved_projection_loader
        if loader is None:
            raise OpenHandsTraceError("OpenHands oversized event has no saved trajectory fallback")
        if self._delivery_cancelled.is_set():
            raise OpenHandsTraceError("OpenHands event delivery was cancelled")
        try:
            saved = loader(final)
        except Exception as exc:
            raise OpenHandsTraceError("OpenHands saved trajectory fallback failed") from exc
        if (
            not isinstance(saved, SavedTrajectoryProjection)
            or not isinstance(saved.steps, tuple)
            or not isinstance(saved.source_event_ids, frozenset)
            or any(
                not isinstance(event_id, str) or _EVENT_FILE.fullmatch(f"{event_id}.json") is None
                for event_id in saved.source_event_ids
            )
        ):
            raise OpenHandsTraceError("OpenHands saved trajectory fallback is malformed")
        if str(oversized_origin.event_id) not in saved.source_event_ids:
            raise OpenHandsTraceError("OpenHands saved trajectory omits the oversized source event")
        return list(saved.steps)

    def _deferred_path_stats(
        self,
        paths: Sequence[tuple[_EventOrigin, Path]],
        oversized_origin: _EventOrigin,
    ) -> tuple[tuple[_EventOrigin, int, int, int, int], ...]:
        """Validate, but never read, the deferred raw suffix after shutdown."""

        in_suffix = False
        witnessed: list[tuple[_EventOrigin, int, int, int, int]] = []
        for origin, path in paths:
            if origin == oversized_origin:
                in_suffix = True
            if not in_suffix:
                continue
            try:
                info = path.lstat()
            except OSError as exc:
                raise OpenHandsTraceError("OpenHands deferred event path is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OpenHandsTraceError("OpenHands deferred event path is not a regular file")
            if origin == oversized_origin and info.st_size <= self._max_event_bytes:
                raise OpenHandsTraceError("OpenHands deferred oversized event changed before finalization")
            witnessed.append((origin, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        return tuple(witnessed)

    @staticmethod
    def _assert_projection_prefix(prefix: Sequence[object], projection: Sequence[object]) -> None:
        """Bind the saved projection to every semantic step decoded live."""

        try:
            prefix_keys = [item.key for item in prefix]  # type: ignore[attr-defined]
            projection_keys = [item.key for item in projection]  # type: ignore[attr-defined]
            prefix_steps = [item.step for item in prefix]  # type: ignore[attr-defined]
            projection_steps = [item.step for item in projection]  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise OpenHandsTraceError("OpenHands saved trajectory projection is malformed") from exc
        if projection_keys[: len(prefix_keys)] != prefix_keys:
            raise OpenHandsTraceError("OpenHands saved trajectory changed the live projection prefix")
        for live, saved in zip(prefix_steps, projection_steps, strict=False):
            if not isinstance(live, Step) or not isinstance(saved, Step):
                raise OpenHandsTraceError("OpenHands saved trajectory projection is malformed")
            if _step_fingerprint(live) != _step_fingerprint(saved):
                raise OpenHandsTraceError("OpenHands saved trajectory changed a live projected step")

    def _decode(
        self,
        payload: bytes,
        *,
        origin: _EventOrigin,
        final: bool,
    ) -> object | None | _Retry:
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if final:
                raise OpenHandsTraceError(
                    f"OpenHands event {origin.session}/{origin.event_id} is incomplete JSON"
                ) from exc
            return _RETRY
        if not isinstance(raw, dict):
            raise OpenHandsTraceError(f"OpenHands event {origin.session}/{origin.event_id} is not an object")
        return self._projector.decode(raw, origin=_render_origin(origin))

    def _emit_new_suffix(self, projection: list[object]) -> None:
        try:
            keys = [item.key for item in projection]  # type: ignore[attr-defined]
            steps = [item.step for item in projection]  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise OpenHandsTraceError("OpenHands projection produced a malformed HUD step") from exc
        if any(not isinstance(key, str) for key in keys) or any(not isinstance(step, Step) for step in steps):
            raise OpenHandsTraceError("OpenHands projection produced a malformed HUD step")
        if any(not key for key in keys):
            raise OpenHandsTraceError("OpenHands projection produced an empty semantic key")
        if any(len(key) > 4096 for key in keys):
            raise OpenHandsTraceError("OpenHands projection produced an oversized semantic key")
        if len(set(keys)) != len(keys):
            raise OpenHandsTraceError("OpenHands projection produced duplicate semantic keys")
        if keys[: len(self._emitted_keys)] != self._emitted_keys:
            raise OpenHandsTraceError("OpenHands projection changed an already-emitted prefix")

        for key, step in zip(
            keys[: len(self._emitted_keys)],
            steps[: len(self._emitted_keys)],
            strict=True,
        ):
            rendered = _step_fingerprint(step)
            if self._emitted_payloads[key] != rendered:
                raise OpenHandsTraceError(f"OpenHands projection changed already-emitted step {key}")

        for key, step in zip(
            keys[len(self._emitted_keys) :],
            steps[len(self._emitted_keys) :],
            strict=True,
        ):
            if self._delivery_cancelled.is_set():
                raise OpenHandsTraceError("OpenHands event delivery was cancelled")
            rendered = _step_fingerprint(step)
            # HUD mutates recorded steps to add IDs/timestamps.  Preserve the
            # projector-owned value so a later reconciliation compares only
            # canonical projection data, not transport mutations.
            delivered = step.model_copy(deep=True)
            self._sink(delivered)
            self._emitted_keys.append(key)
            self._emitted_payloads[key] = rendered
            self._emitted_step_counts[step.source] += 1

    def _event_paths(self, *, final: bool) -> list[tuple[_EventOrigin, Path]]:
        sessions_root = self._receipt_log_dir / "file" / "sessions"
        try:
            root_stat = sessions_root.lstat()
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise OpenHandsTraceError("OpenHands sessions root is not a real directory")

        sessions: dict[str, list[tuple[_EventOrigin, Path]]] = {}
        for session_dir in sorted(sessions_root.iterdir(), key=lambda path: path.name):
            session_stat = session_dir.lstat()
            if stat.S_ISLNK(session_stat.st_mode):
                raise OpenHandsTraceError("OpenHands session directory may not be a symlink")
            if not stat.S_ISDIR(session_stat.st_mode):
                continue
            events_dir = session_dir / "events"
            try:
                events_stat = events_dir.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(events_stat.st_mode) or stat.S_ISLNK(events_stat.st_mode):
                raise OpenHandsTraceError("OpenHands events path is not a real directory")
            found: list[tuple[_EventOrigin, Path]] = []
            for path in events_dir.iterdir():
                match = _EVENT_FILE.fullmatch(path.name)
                if match is None:
                    continue
                found.append(
                    (
                        _EventOrigin(
                            session=session_dir.name,
                            event_id=int(match.group("event_id")),
                        ),
                        path,
                    )
                )
            if found:
                sessions[session_dir.name] = found

        if not sessions:
            if final:
                raise OpenHandsTraceError("OpenHands event store has no event session")
            return []
        if len(sessions) != 1:
            raise OpenHandsTraceError("OpenHands event store has multiple nonempty sessions")

        found = next(iter(sessions.values()))
        found.sort(key=lambda item: item[0].event_id)
        origins = [origin for origin, _path in found]
        if len(set(origins)) != len(origins):
            raise OpenHandsTraceError("OpenHands event store contains duplicate event identities")
        event_ids = [origin.event_id for origin in origins]
        if final:
            if any(event_id != expected for expected, event_id in enumerate(event_ids)):
                raise OpenHandsTraceError("OpenHands final event IDs are not contiguous from zero")
            return found

        # Event writes can complete out of order because EventStream assigns
        # IDs under a lock and writes outside it.  Only expose the contiguous
        # prefix; a later scan will pick up the suffix after its gap closes.
        prefix_length = 0
        for expected, event_id in enumerate(event_ids):
            if event_id != expected:
                break
            prefix_length += 1
        return found[:prefix_length]

    def _read_event_file(self, path: Path) -> tuple[bytes, str]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise OpenHandsTraceError("could not open OpenHands event file safely") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OpenHandsTraceError("OpenHands event path is not a regular file")
            if before.st_size > self._max_event_bytes:
                raise _OversizedEventFile
            chunks: list[bytes] = []
            remaining = self._max_event_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > self._max_event_bytes:
                raise _OversizedEventFile
            after = os.fstat(descriptor)
            if (
                len(payload) != before.st_size
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ino != after.st_ino
            ):
                raise _UnstableEventFile
        finally:
            os.close(descriptor)
        return payload, hashlib.sha256(payload).hexdigest()


class _Retry:
    pass


class _UnstableEventFile(RuntimeError):
    pass


class _OversizedEventFile(RuntimeError):
    pass


_RETRY = _Retry()


def _render_origin(origin: _EventOrigin) -> str:
    return f"sessions/{origin.session}/events/{origin.event_id}.json"


def _step_fingerprint(step: Step) -> str:
    payload = step.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "OpenHandsEventProjector",
    "OpenHandsEventTailer",
    "OpenHandsTraceError",
]

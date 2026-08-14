"""Crash-safe, no-model historical HUD step backfill.

The source-specific trajectory mapper is deliberately outside this module.  A
mapper is a pure callable that accepts a local path and returns stable keys
paired with already structured HUD ``Step`` objects.  This module owns only the
generic safety boundary: defensive redaction, deterministic span construction,
terminal-trace checks, direct telemetry upload, remote event verification, and
a durable local ledger.

Dry-run is the CLI default.  ``--apply`` is the only path that performs network
writes, and credentials are read from ``HUD_API_KEY`` rather than argv.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from hud.agents.types import AgentStep, ToolStep, Usage
from hud.settings import settings
from hud.telemetry.span import (
    PAYLOAD_ATTRIBUTE,
    SCHEMA_ATTRIBUTE,
    TASK_RUN_ID_ATTRIBUTE,
    Span,
    normalize_trace_id,
)
from hud.types import MCPToolCall, MCPToolResult, Step
from mcp.types import PromptMessage, TextContent

BACKFILL_SCHEMA_VERSION = "1"
DEFAULT_NAMESPACE = "cybergym-openhands-history-v1"
TERMINAL_TRACE_STATUSES = frozenset({"completed", "error", "cancelled"})
_MAX_KEY_LENGTH = 512
_MAX_BATCH_SPANS = 100
_MAX_BATCH_BYTES = 4 * 1024 * 1024
_SECRET_ENV_SUFFIXES = (
    "_API_KEY",
    "_AUTH",
    "_AUTHORIZATION",
    "_COOKIE",
    "_COOKIES",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_KEY",
    "_PASSWORD",
    "_SECRET",
    "_TOKEN",
)
_EXPLICIT_SECRET_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "DAYTONA_API_KEY",
        "HUD_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
    }
)
_GENERIC_SECRET_ENV_NAMES = frozenset(
    {
        "ACCESS_TOKEN",
        "API_KEY",
        "AUTH",
        "AUTHORIZATION",
        "COOKIE",
        "COOKIES",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWORD",
        "PRIVATE_KEY",
        "REFRESH_TOKEN",
        "SECRET",
        "TOKEN",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "agent_id",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "checksum",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "flag",
        "github_pat",
        "github_token",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "token",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_access_token",
    "_agent_id",
    "_api_key",
    "_checksum",
    "_client_secret",
    "_cookie",
    "_cookies",
    "_credential",
    "_credentials",
    "_flag",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_session",
    "_session_id",
    "_token",
)
_ASSIGNMENT_KEY = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|bearer|"
    r"client[_-]?secret|cookie(?:s)?|credential(?:s)?|flag|github[_-]?(?:pat|token)|session(?:[_-]?id)?|"
    r"password|passwd|private[_-]?key|secret|token|"
    r"[a-z][a-z0-9_-]*(?:api[_-]?key|auth|cookie|credential|password|secret|token))"
)
_QUOTED_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>(?<![\w-])[\"']?{_ASSIGNMENT_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\"'\r\n]{1,8192})(?P=quote)"
)
_BARE_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>(?<![\w-])[\"']?{_ASSIGNMENT_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<value>[^\s,;}\]\r\n\"']{1,8192})"
)
_STRING_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "authorization",
        re.compile(r"(?i)\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1[REDACTED:authorization]",
    ),
    (
        "cookie_header",
        re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]{1,16384}"),
        r"\1[REDACTED:cookie_header]",
    ),
    (
        "jwt",
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        "[REDACTED:jwt]",
    ),
    (
        "provider_key",
        re.compile(
            r"(?<![A-Za-z0-9])(?:sk-(?:ant-)?|sess-|gh[pousr]_|github_pat_|glpat-|hf_|xox[baprs]-|"
            r"AIza|AKIA|ASIA)[A-Za-z0-9_-]{12,}"
        ),
        "[REDACTED:provider_key]",
    ),
    (
        "challenge_flag",
        re.compile(r"(?i)\b(?:flag|ctf)\{[^{}\r\n]{1,512}\}"),
        "[REDACTED:challenge_flag]",
    ),
    (
        "url_credentials",
        re.compile(r"(?i)\b(https?://)[^/@\s:]+:[^/@\s]+@"),
        r"\1[REDACTED:url_credentials]@",
    ),
)

_APPROVED_API_HOSTS = frozenset({"api.hud.ai", "api.beta.hud.ai"})
_APPROVED_TELEMETRY_HOSTS = frozenset({"telemetry.hud.ai"})


class TraceBackfillError(RuntimeError):
    """A safe-to-display backfill diagnostic."""


@dataclass(frozen=True, slots=True)
class KeyedStep:
    """One mapper output with a stable, source-local identity key."""

    key: str
    step: Step


class StepMapper(Protocol):
    """Pure source mapper seam consumed by the backfill CLI."""

    def __call__(self, source: Path) -> Mapping[str, Step] | Iterable[object]: ...


class BackfillTransport(Protocol):
    """Minimal remote seam, kept injectable so tests never contact HUD."""

    def fetch_events(self, trace_id: str) -> Mapping[str, Any]: ...

    def upload_spans(self, trace_id: str, spans: Sequence[dict[str, Any]]) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class RedactionReport:
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def record(self, category: str, count: int = 1) -> None:
        if count > 0:
            self.counts[category] += count

    def public(self) -> dict[str, Any]:
        return {
            "applied": self.total,
            "by_category": dict(sorted(self.counts.items())),
        }


class StepRedactor:
    """Generic defense-in-depth redactor for mapper-produced step payloads.

    Source mappers remain responsible for benchmark-specific policy.  This
    layer catches common credential/flag shapes, secret-looking mapping keys,
    and literal secret values currently present in the process environment.
    It never prints or persists the matched values.
    """

    def __init__(self, secret_literals: Iterable[str] = ()) -> None:
        self._secret_literals = tuple(
            sorted(
                {value for value in secret_literals if isinstance(value, str) and len(value) >= 8},
                key=len,
                reverse=True,
            )
        )

    @classmethod
    def from_environment(cls) -> StepRedactor:
        values = [
            value
            for name, value in os.environ.items()
            if value
            and (
                name.upper() in _EXPLICIT_SECRET_ENV_NAMES
                or name.upper() in _GENERIC_SECRET_ENV_NAMES
                or name.upper().endswith(_SECRET_ENV_SUFFIXES)
            )
        ]
        return cls(values)

    def sanitize(self, step: Step) -> tuple[Step, RedactionReport]:
        step = _whitelist_step(step)
        report = RedactionReport()
        payload = _wire_payload(step)
        sanitized = self._value(payload, report)
        try:
            rebuilt = _step_from_wire_payload(sanitized)
        except Exception as exc:
            raise TraceBackfillError(
                f"redaction made {type(step).__name__} invalid ({type(exc).__name__})"
            ) from None
        return cast("Step", rebuilt), report

    def _value(self, value: Any, report: RedactionReport, *, key: str | None = None) -> Any:
        if key is not None and _is_sensitive_key(key) and value is not None:
            report.record("sensitive_key")
            return "[REDACTED:sensitive_key]"
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for item_key, item in value.items():
                original_key = str(item_key)
                safe_key = self._text(original_key, report)
                if safe_key in sanitized:
                    raise TraceBackfillError("redaction collapsed distinct mapping keys")
                sanitized[safe_key] = self._value(item, report, key=original_key)
            return sanitized
        if isinstance(value, list):
            return [self._value(item, report) for item in value]
        if isinstance(value, str):
            return self._text(value, report)
        return value

    def _text(self, value: str, report: RedactionReport) -> str:
        rendered = value
        for secret in self._secret_literals:
            occurrences = rendered.count(secret)
            if occurrences:
                rendered = rendered.replace(secret, "[REDACTED:environment_literal]")
                report.record("environment_literal", occurrences)
        for category, pattern, replacement in _STRING_PATTERNS:
            rendered, count = pattern.subn(replacement, rendered)
            report.record(category, count)
        rendered, count = _QUOTED_ASSIGNMENT.subn(
            lambda match: (
                f"{match.group('prefix')}{match.group('quote')}"
                f"[REDACTED:assignment]{match.group('quote')}"
            ),
            rendered,
        )
        report.record("assignment", count)
        rendered, count = _BARE_ASSIGNMENT.subn(
            lambda match: f"{match.group('prefix')}[REDACTED:assignment]",
            rendered,
        )
        report.record("assignment", count)
        return rendered


@dataclass(frozen=True, slots=True)
class EventExpectation:
    span_id: str
    kind: str
    agent_must_be_visible: bool = False
    tool_name: str | None = None
    projection_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    trace_id: str
    namespace: str
    spans: tuple[dict[str, Any], ...]
    expectations: tuple[EventExpectation, ...]
    plan_sha256: str
    payload_bytes: int
    event_counts: Mapping[str, int]
    redactions: Mapping[str, Any]

    @property
    def span_ids(self) -> tuple[str, ...]:
        return tuple(expectation.span_id for expectation in self.expectations)

    def public_summary(self, *, mode: str) -> dict[str, Any]:
        return {
            "schema_version": BACKFILL_SCHEMA_VERSION,
            "mode": mode,
            "trace_id": self.trace_id,
            "namespace": self.namespace,
            "plan_sha256": self.plan_sha256,
            "span_count": len(self.spans),
            "payload_bytes": self.payload_bytes,
            "event_counts": dict(sorted(self.event_counts.items())),
            "redactions": dict(self.redactions),
            "contains_step_content": False,
            "network_write_performed": False,
        }


def build_backfill_plan(
    trace_id: str,
    mapped: Mapping[str, Step] | Iterable[KeyedStep],
    *,
    namespace: str = DEFAULT_NAMESPACE,
    redactor: StepRedactor | None = None,
) -> BackfillPlan:
    """Sanitize mapper output and deterministically encode HUD telemetry spans."""

    try:
        canonical_trace_id = str(UUID(trace_id))
    except (TypeError, ValueError, AttributeError):
        raise TraceBackfillError("trace ID must be a UUID") from None
    if (
        not namespace
        or namespace != namespace.strip()
        or len(namespace) > 256
        or any(ord(character) < 32 for character in namespace)
    ):
        raise TraceBackfillError("backfill namespace must be non-empty printable text with no surrounding whitespace")
    keyed_steps = _normalize_mapped_steps(mapped)
    if not keyed_steps:
        raise TraceBackfillError("mapper returned no HUD steps")
    active_redactor = redactor or StepRedactor.from_environment()
    combined_report = RedactionReport()
    seen_keys: set[str] = set()
    seen_span_ids: set[str] = set()
    whitelisted_items: list[KeyedStep] = []

    for item in keyed_steps:
        _validate_key(item.key)
        if item.key in seen_keys:
            raise TraceBackfillError("mapper returned a duplicate stable step key")
        seen_keys.add(item.key)
        if not isinstance(item.step, Step):
            raise TraceBackfillError("mapper returned a value that is not a HUD Step")
        whitelisted_step = _whitelist_step(item.step)
        _validate_step(whitelisted_step)
        whitelisted_items.append(KeyedStep(item.key, whitelisted_step))

    # Validate the original selected values before redaction can intentionally
    # collapse sensitive strings to the same placeholder.
    _validate_topology(whitelisted_items)
    sanitized_items: list[KeyedStep] = []
    for item in whitelisted_items:
        sanitized_step, report = active_redactor.sanitize(item.step)
        combined_report.counts.update(report.counts)
        _validate_step(sanitized_step)
        sanitized_items.append(KeyedStep(item.key, sanitized_step))

    _validate_topology(sanitized_items)
    sanitized_items = _strictly_monotonic_steps(sanitized_items)
    spans: list[dict[str, Any]] = []
    expectations: list[EventExpectation] = []
    for item in sanitized_items:
        sanitized_step = item.step
        span_id = deterministic_span_id(canonical_trace_id, namespace, item.key)
        if span_id in seen_span_ids:
            raise TraceBackfillError("deterministic step IDs collided; choose a different namespace")
        seen_span_ids.add(span_id)
        payload = _wire_payload(sanitized_step)
        span = Span(
            name=f"step.{sanitized_step.source}",
            trace_id=normalize_trace_id(canonical_trace_id),
            span_id=span_id,
            start_time=cast("str", sanitized_step.started_at),
            end_time=cast("str", sanitized_step.ended_at),
            status_code="ERROR" if sanitized_step.error else "OK",
            status_message=sanitized_step.error,
            attributes={
                SCHEMA_ATTRIBUTE: sanitized_step.schema_tag,
                TASK_RUN_ID_ATTRIBUTE: canonical_trace_id,
                PAYLOAD_ATTRIBUTE: payload,
            },
        ).model_dump(mode="json")
        spans.append(span)
        expectations.append(_expectation(sanitized_step, span_id))

    encoded_spans = [_canonical_json(span) for span in spans]
    payload_bytes = sum(len(encoded) for encoded in encoded_spans)
    digest = hashlib.sha256(b"\n".join(encoded_spans)).hexdigest()
    counts = Counter(expectation.kind for expectation in expectations)
    return BackfillPlan(
        trace_id=canonical_trace_id,
        namespace=namespace,
        spans=tuple(spans),
        expectations=tuple(expectations),
        plan_sha256=digest,
        payload_bytes=payload_bytes,
        event_counts=dict(counts),
        redactions=combined_report.public(),
    )


def deterministic_span_id(trace_id: str, namespace: str, key: str) -> str:
    """Return the stable 16-hex OTel span ID for one source-local key."""

    try:
        canonical_trace_id = str(UUID(trace_id))
    except (TypeError, ValueError, AttributeError):
        raise TraceBackfillError("trace ID must be a UUID") from None
    material = f"{namespace}\0{canonical_trace_id}\0{key}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


class HTTPBackfillTransport:
    """Direct HUD HTTP transport with no best-effort exporter swallowing."""

    def __init__(
        self,
        *,
        api_url: str,
        telemetry_url: str,
        api_key: str,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise TraceBackfillError("HUD_API_KEY is required for --apply")
        self._api_root = _versioned_api_root(api_url)
        self._telemetry_root = _safe_hud_root(
            telemetry_url,
            label="HUD telemetry URL",
            approved_hosts=_APPROVED_TELEMETRY_HOSTS,
        )
        self._headers = {"Authorization": f"Bearer {api_key}"}
        if client is not None and client.follow_redirects:
            raise TraceBackfillError("HUD backfill HTTP client may not follow redirects")
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_events(self, trace_id: str) -> Mapping[str, Any]:
        url = f"{self._api_root}/trace/{trace_id}/events"
        return self._request_json("GET", url)

    def upload_spans(self, trace_id: str, spans: Sequence[dict[str, Any]]) -> Mapping[str, Any]:
        url = f"{self._telemetry_root}/trace/{trace_id}/telemetry-upload"
        response = self._request_json("POST", url, payload={"telemetry": list(spans)})
        if response.get("status") != "accepted" or response.get("count") != len(spans):
            raise TraceBackfillError("HUD telemetry upload returned a malformed acceptance receipt")
        return response

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = self._client.request(method, url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise TraceBackfillError(f"HUD {method} request failed ({type(exc).__name__})") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise TraceBackfillError(f"HUD {method} request returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise TraceBackfillError(f"HUD {method} response was not JSON") from None
        if not isinstance(body, dict):
            raise TraceBackfillError(f"HUD {method} response was not an object")
        return cast("Mapping[str, Any]", body)


class BackfillLedger:
    """Atomic, fsync-backed, mode-0600 local idempotency journal."""

    def __init__(self, path: Path) -> None:
        # ``resolve`` would follow an attacker-controlled final symlink before
        # our lstat checks.  Keep the absolute lexical path instead.
        self.path = Path(os.path.abspath(path.expanduser()))
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def locked(self):
        try:
            self._prepare_parent()
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_path, flags, 0o600)
        except TraceBackfillError:
            raise
        except OSError as exc:
            raise TraceBackfillError(f"could not open backfill ledger lock ({type(exc).__name__})") from None
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise TraceBackfillError("backfill ledger must be a regular non-symlink file")
        if not self.path.exists():
            return {"schema_version": BACKFILL_SCHEMA_VERSION, "traces": {}}
        self._require_safe_regular_file(self.path)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TraceBackfillError(f"backfill ledger is unreadable ({type(exc).__name__})") from None
        if not isinstance(payload, dict) or payload.get("schema_version") != BACKFILL_SCHEMA_VERSION:
            raise TraceBackfillError("backfill ledger schema is unsupported")
        if not isinstance(payload.get("traces"), dict):
            raise TraceBackfillError("backfill ledger has a malformed traces map")
        return payload

    def save(self, payload: Mapping[str, Any]) -> None:
        self._prepare_parent()
        encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".trace-backfill-", dir=self.path.parent)
        except OSError as exc:
            raise TraceBackfillError(
                f"could not create backfill ledger temporary file ({type(exc).__name__})"
            ) from None
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, 0o600)
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise TraceBackfillError("short write while persisting the backfill ledger")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            except TraceBackfillError:
                raise
            except OSError as exc:
                raise TraceBackfillError(f"could not write backfill ledger ({type(exc).__name__})") from None
            finally:
                os.close(descriptor)
            try:
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as exc:
                raise TraceBackfillError(f"could not commit backfill ledger ({type(exc).__name__})") from None
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                # A mode-0600 orphan is safer than hiding the primary durable
                # write failure. The next operator can inspect/delete it.
                pass
        self._require_safe_regular_file(self.path)

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        _reject_symlink_components(parent)
        if parent.exists() and parent.is_symlink():
            raise TraceBackfillError("backfill ledger directory may not be a symlink")
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise TraceBackfillError(f"could not create backfill ledger directory ({type(exc).__name__})") from None
        mode = stat.S_IMODE(parent.stat(follow_symlinks=False).st_mode)
        if mode & 0o077:
            raise TraceBackfillError("backfill ledger directory must not be accessible to group or other users")
        os.chmod(parent, 0o700)
        if self.lock_path.is_symlink():
            raise TraceBackfillError("backfill ledger lock may not be a symlink")
        if self.path.is_symlink():
            raise TraceBackfillError("backfill ledger must be a regular non-symlink file")

    @staticmethod
    def _require_safe_regular_file(path: Path) -> None:
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise TraceBackfillError(f"could not inspect backfill ledger ({type(exc).__name__})") from None
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise TraceBackfillError("backfill ledger must be a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TraceBackfillError("backfill ledger must have mode 0600")


def apply_backfill(
    plan: BackfillPlan,
    *,
    ledger: BackfillLedger,
    transport: BackfillTransport,
    verify_timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """Upload only remotely missing deterministic spans and verify projection."""

    if verify_timeout_seconds < 0 or poll_interval_seconds < 0:
        raise TraceBackfillError("verification timing values may not be negative")
    with ledger.locked():
        state = ledger.load()
        traces = cast("dict[str, Any]", state["traces"])
        prior = traces.get(plan.trace_id)
        if isinstance(prior, dict) and prior.get("plan_sha256") != plan.plan_sha256:
            raise TraceBackfillError(
                "this trace already has a different ledger plan; use a separate ledger after review"
            )

        initial = _fetch_remote_snapshot(transport, plan)
        if initial.status not in TERMINAL_TRACE_STATUSES:
            raise TraceBackfillError(f"refusing telemetry backfill for nonterminal trace status {initial.status!r}")
        if initial.wrong_kind_ids or initial.wrong_order:
            raise TraceBackfillError(
                "remote trace has deterministic event IDs with unexpected order, kinds, or visible payloads"
            )

        now = _now()
        entry: dict[str, Any] = {
            "schema_version": BACKFILL_SCHEMA_VERSION,
            "trace_id": plan.trace_id,
            "namespace": plan.namespace,
            "plan_sha256": plan.plan_sha256,
            "span_ids": list(plan.span_ids),
            "span_count": len(plan.spans),
            "event_counts": dict(sorted(plan.event_counts.items())),
            "redactions": dict(plan.redactions),
            "status": "prepared",
            "prepared_at": prior.get("prepared_at", now) if isinstance(prior, dict) else now,
            "updated_at": now,
            "remote_status": initial.status,
            "remote_present_before": len(initial.present_ids),
            "upload_receipts": prior.get("upload_receipts", []) if isinstance(prior, dict) else [],
        }
        traces[plan.trace_id] = entry
        ledger.save(state)

        if not initial.missing_ids:
            _require_complete_snapshot(initial, plan)
            entry.update(
                {
                    "status": "verified",
                    "verified_at": _now(),
                    "updated_at": _now(),
                    "remote_verified_count": len(initial.present_ids),
                    "network_write_performed": False,
                }
            )
            ledger.save(state)
            return _apply_summary(
                plan,
                initial,
                uploaded=0,
                already_present=len(initial.present_ids),
                network_write_performed=False,
            )

        spans_by_id = {cast("str", span["span_id"]): span for span in plan.spans}
        missing_spans = [spans_by_id[span_id] for span_id in plan.span_ids if span_id in initial.missing_ids]
        uploaded = 0
        upload_attempted = False
        entry["status"] = "uploading"
        entry["updated_at"] = _now()
        ledger.save(state)

        try:
            for batch in _batches(missing_spans):
                upload_attempted = True
                receipt = transport.upload_spans(plan.trace_id, batch)
                uploaded += len(batch)
                cast("list[dict[str, Any]]", entry["upload_receipts"]).append(
                    {
                        "status": receipt.get("status"),
                        "count": receipt.get("count"),
                        "sequence": receipt.get("sequence"),
                    }
                )
                entry["uploaded_span_count"] = uploaded
                entry["updated_at"] = _now()
                ledger.save(state)
        except Exception as exc:
            entry["status"] = "upload_outcome_ambiguous"
            entry["last_error"] = f"upload failed ({type(exc).__name__})"
            entry["updated_at"] = _now()
            ledger.save(state)
            reconciled = _wait_for_complete_snapshot(
                transport,
                plan,
                timeout_seconds=verify_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            if reconciled is not None:
                entry.update(
                    {
                        "status": "verified",
                        "verified_at": _now(),
                        "updated_at": _now(),
                        "remote_verified_count": len(reconciled.present_ids),
                        "network_write_performed": True,
                    }
                )
                entry.pop("last_error", None)
                ledger.save(state)
                return _apply_summary(
                    plan,
                    reconciled,
                    uploaded=uploaded,
                    already_present=len(initial.present_ids),
                    network_write_performed=upload_attempted,
                )
            raise TraceBackfillError(
                "telemetry upload outcome is ambiguous and remote verification is incomplete; rerun safely"
            ) from None

        verified = _wait_for_complete_snapshot(
            transport,
            plan,
            timeout_seconds=verify_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if verified is None:
            entry["status"] = "verification_pending"
            entry["updated_at"] = _now()
            ledger.save(state)
            raise TraceBackfillError("HUD did not project every deterministic backfill event before timeout")
        entry.update(
            {
                "status": "verified",
                "verified_at": _now(),
                "updated_at": _now(),
                "remote_verified_count": len(verified.present_ids),
                "network_write_performed": True,
            }
        )
        ledger.save(state)
        return _apply_summary(
            plan,
            verified,
            uploaded=uploaded,
            already_present=len(initial.present_ids),
            network_write_performed=upload_attempted,
        )


@dataclass(frozen=True, slots=True)
class _RemoteSnapshot:
    status: str
    present_ids: frozenset[str]
    missing_ids: frozenset[str]
    wrong_kind_ids: frozenset[str]
    wrong_order: bool
    planned_counts: Mapping[str, int]


def _fetch_remote_snapshot(transport: BackfillTransport, plan: BackfillPlan) -> _RemoteSnapshot:
    try:
        body = transport.fetch_events(plan.trace_id)
    except TraceBackfillError:
        raise
    except Exception as exc:
        raise TraceBackfillError(f"HUD event verification failed ({type(exc).__name__})") from None
    status = body.get("status")
    events = body.get("events")
    if not isinstance(status, str) or not isinstance(events, list):
        raise TraceBackfillError("HUD events response has a malformed status or events list")
    by_id: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            continue
        event_id = cast("str", event["id"])
        if event_id in by_id:
            raise TraceBackfillError("HUD events response contains duplicate projected event IDs")
        by_id[event_id] = event

    expectation_by_id = {expectation.span_id: expectation for expectation in plan.expectations}
    present = frozenset(expectation_by_id).intersection(by_id)
    missing = frozenset(expectation_by_id).difference(by_id)
    remote_order = tuple(
        cast("str", event["id"])
        for event in events
        if isinstance(event, dict)
        and isinstance(event.get("id"), str)
        and event.get("id") in expectation_by_id
    )
    expected_present_order = tuple(span_id for span_id in plan.span_ids if span_id in present)
    is_prefix = expected_present_order == plan.span_ids[: len(expected_present_order)]
    wrong_order = remote_order != expected_present_order or not is_prefix
    wrong: set[str] = set()
    counts: Counter[str] = Counter()
    for span_id in present:
        expected = expectation_by_id[span_id]
        event = by_id[span_id]
        kind = event.get("kind")
        if kind != expected.kind:
            wrong.add(span_id)
            continue
        counts[expected.kind] += 1
        if expected.agent_must_be_visible:
            text = event.get("text")
            reasoning = event.get("reasoning")
            tool_calls = event.get("tool_calls")
            if not (
                (isinstance(text, str) and bool(text.strip()))
                or (isinstance(reasoning, str) and bool(reasoning.strip()))
                or (isinstance(tool_calls, list) and bool(tool_calls))
            ):
                wrong.add(span_id)
        if expected.tool_name is not None and event.get("tool_name") != expected.tool_name:
            wrong.add(span_id)
        if expected.projection_sha256 is not None:
            projected = _projection_view_from_event(expected.kind, event)
            if hashlib.sha256(_canonical_json(projected)).hexdigest() != expected.projection_sha256:
                wrong.add(span_id)
    return _RemoteSnapshot(
        status=status,
        present_ids=frozenset(present),
        missing_ids=missing,
        wrong_kind_ids=frozenset(wrong),
        wrong_order=wrong_order,
        planned_counts=dict(counts),
    )


def _wait_for_complete_snapshot(
    transport: BackfillTransport,
    plan: BackfillPlan,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> _RemoteSnapshot | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = _fetch_remote_snapshot(transport, plan)
        if snapshot.status not in TERMINAL_TRACE_STATUSES:
            raise TraceBackfillError("trace left terminal status during backfill verification")
        if snapshot.wrong_kind_ids or snapshot.wrong_order:
            raise TraceBackfillError("HUD projected deterministic event IDs with unexpected content shape")
        if not snapshot.missing_ids:
            _require_complete_snapshot(snapshot, plan)
            return snapshot
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def _require_complete_snapshot(snapshot: _RemoteSnapshot, plan: BackfillPlan) -> None:
    if snapshot.missing_ids or snapshot.wrong_kind_ids or snapshot.wrong_order:
        raise TraceBackfillError("remote backfill projection is incomplete")
    expected = Counter(expectation.kind for expectation in plan.expectations)
    observed = Counter(snapshot.planned_counts)
    if observed != expected:
        raise TraceBackfillError(
            "remote deterministic event counts do not match the local agent/tool/user plan"
        )


def _apply_summary(
    plan: BackfillPlan,
    snapshot: _RemoteSnapshot,
    *,
    uploaded: int,
    already_present: int,
    network_write_performed: bool,
) -> dict[str, Any]:
    summary = plan.public_summary(mode="apply")
    summary.update(
        {
            "remote_status": snapshot.status,
            "remote_verified": True,
            "remote_verified_event_counts": dict(sorted(snapshot.planned_counts.items())),
            "uploaded_span_count": uploaded,
            "already_present_span_count": already_present,
            "network_write_performed": network_write_performed,
        }
    )
    return summary


def _normalize_mapped_steps(mapped: Mapping[str, Step] | Iterable[object]) -> list[KeyedStep]:
    if isinstance(mapped, Mapping):
        return [KeyedStep(key=str(key), step=step) for key, step in mapped.items()]
    try:
        values = list(mapped)
    except TypeError:
        raise TraceBackfillError("mapper result is neither a mapping nor an iterable of KeyedStep") from None
    normalized: list[KeyedStep] = []
    for value in values:
        if isinstance(value, KeyedStep):
            normalized.append(value)
            continue
        # Accept a source mapper's own frozen/dataclass ``ProjectedStep``
        # without importing it here. This is the entire integration seam.
        key = getattr(value, "key", None)
        step = getattr(value, "step", None)
        if not isinstance(key, str) or not isinstance(step, Step):
            raise TraceBackfillError("mapper iterable values must expose string key and HUD Step attributes")
        normalized.append(KeyedStep(key=key, step=step))
    return normalized


def _validate_key(key: str) -> None:
    if not key or key != key.strip() or len(key) > _MAX_KEY_LENGTH:
        raise TraceBackfillError("stable mapper keys must be 1-512 characters with no surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in key):
        raise TraceBackfillError("stable mapper keys may not contain control characters")


def _whitelist_step(step: Step) -> Step:
    """Copy only display-grade CyberGym channels and reject hidden payloads."""

    if step.step_id is not None or step.task_call is not None or step.error is not None or step.extra:
        raise TraceBackfillError("mapper step contains forbidden metadata, task state, extra, or errors")
    if step.source == "user":
        if isinstance(step, (AgentStep, ToolStep)):
            raise TraceBackfillError("user-sourced mapper values must be plain Step objects")
        messages: list[PromptMessage] = []
        if not step.messages:
            raise TraceBackfillError("mapper produced a blank HUD user message")
        for message in step.messages:
            if not isinstance(message, PromptMessage) or message.role != "user":
                raise TraceBackfillError("user mapper messages must be user-role PromptMessage objects")
            if _model_extras(message):
                raise TraceBackfillError("user PromptMessage contains unknown metadata")
            content = message.content
            if not isinstance(content, TextContent):
                raise TraceBackfillError("user mapper messages may contain text only")
            if content.annotations is not None or content.meta is not None or _model_extras(content):
                raise TraceBackfillError("user text content contains annotations or unknown metadata")
            if not content.text.strip():
                raise TraceBackfillError("mapper produced a blank HUD user message")
            messages.append(
                PromptMessage(role="user", content=TextContent(type="text", text=content.text))
            )
        return Step(
            source="user",
            messages=messages,
            started_at=step.started_at,
            ended_at=step.ended_at,
        )
    if step.source == "agent":
        if not isinstance(step, AgentStep):
            raise TraceBackfillError("agent-sourced mapper values must be AgentStep objects")
        if (
            step.messages
            or step.raw is not None
            or step.sample is not None
            or step.refusal is not None
            or step.citations
        ):
            raise TraceBackfillError(
                "agent mapper step contains forbidden messages, raw/sample, refusal, or citations"
            )
        calls = [_whitelist_call(call, label="agent tool call") for call in step.tool_calls]
        usage = _whitelist_usage(step.usage)
        return AgentStep(
            content=step.content,
            reasoning=step.reasoning,
            tool_calls=calls,
            model=step.model,
            usage=usage,
            done=step.done,
            finish_reason=step.finish_reason,
            started_at=step.started_at,
            ended_at=step.ended_at,
        )
    if step.source == "tool":
        if not isinstance(step, ToolStep) or step.call is None or step.result is None:
            raise TraceBackfillError("tool-sourced mapper values must be complete ToolStep objects")
        if step.messages:
            raise TraceBackfillError("tool mapper step contains forbidden messages")
        call = _whitelist_call(step.call, label="tool result call")
        result = step.result
        if result.meta is not None or result.structuredContent is not None or _model_extras(result):
            raise TraceBackfillError("tool result contains structured, meta, or unknown extra content")
        if result.call_id is not None and result.call_id != call.id:
            raise TraceBackfillError("tool result call_id does not match its call")
        if not result.content:
            raise TraceBackfillError("tool result must contain text content")
        content: list[TextContent] = []
        for item in result.content:
            if not isinstance(item, TextContent):
                raise TraceBackfillError("tool results may contain text content only")
            if item.annotations is not None or item.meta is not None or _model_extras(item):
                raise TraceBackfillError("tool result text contains annotations or unknown metadata")
            content.append(TextContent(type="text", text=item.text))
        return ToolStep(
            call=call,
            result=MCPToolResult(content=content, isError=bool(result.isError)),
            started_at=step.started_at,
            ended_at=step.ended_at,
        )
    raise TraceBackfillError("historical backfill accepts only user, agent, and tool steps")


def _whitelist_call(call: MCPToolCall, *, label: str) -> MCPToolCall:
    if not isinstance(call.id, str) or not call.id.strip() or not isinstance(call.name, str) or not call.name.strip():
        raise TraceBackfillError(f"{label} must have nonblank string id and name")
    if not isinstance(call.arguments, dict):
        raise TraceBackfillError(f"{label} arguments must be parsed JSON objects")
    if call.task is not None or call.meta is not None or call.annotation is not None or _model_extras(call):
        raise TraceBackfillError(f"{label} contains task, meta, annotation, or unknown metadata")
    if call.provider_name not in {None, call.name}:
        raise TraceBackfillError(f"{label} provider metadata disagrees with its public name")
    _validate_json_value(call.arguments, label=f"{label} arguments")
    return MCPToolCall(id=call.id, name=call.name, arguments=call.arguments)


def _whitelist_usage(usage: Usage | None) -> Usage | None:
    if usage is None:
        return None
    if _model_extras(usage):
        raise TraceBackfillError("agent usage contains unknown metadata")
    values = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_tokens": usage.cached_tokens,
        "cost_usd": usage.cost_usd,
        "llm_call_count": usage.llm_call_count,
    }
    for name, value in values.items():
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            raise TraceBackfillError(f"agent usage {name} must be a nonnegative number")
        if isinstance(value, float) and not math.isfinite(value):
            raise TraceBackfillError(f"agent usage {name} must be finite")
    return Usage(**values)


def _model_extras(value: Any) -> Mapping[str, Any]:
    extras = getattr(value, "__pydantic_extra__", None)
    return extras if isinstance(extras, Mapping) else {}


def _validate_json_value(value: Any, *, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceBackfillError(f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, label=label)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TraceBackfillError(f"{label} contains a non-string mapping key")
            _validate_json_value(item, label=label)
        return
    raise TraceBackfillError(f"{label} contains a non-JSON value")


def _wire_payload(step: Step) -> dict[str, Any]:
    common: dict[str, Any] = {
        "source": step.source,
        "started_at": step.started_at,
        "ended_at": step.ended_at,
    }
    if step.source == "user":
        common["messages"] = [
            {"role": "user", "content": {"type": "text", "text": message.content.text}}
            for message in step.messages
            if isinstance(message.content, TextContent)
        ]
        return common
    if isinstance(step, AgentStep):
        common.update(
            {
                "content": step.content,
                "reasoning": step.reasoning,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in step.tool_calls
                ],
                "done": step.done,
                "finish_reason": step.finish_reason,
                "model": step.model,
                "usage": (
                    step.usage.model_dump(mode="json", exclude_none=True) if step.usage is not None else None
                ),
            }
        )
        return {key: value for key, value in common.items() if value is not None}
    if isinstance(step, ToolStep) and step.call is not None and step.result is not None:
        common.update(
            {
                "call": {
                    "id": step.call.id,
                    "name": step.call.name,
                    "arguments": step.call.arguments,
                },
                "result": {
                    "content": [
                        {"type": "text", "text": item.text}
                        for item in step.result.content
                        if isinstance(item, TextContent)
                    ],
                    "isError": bool(step.result.isError),
                },
            }
        )
        return common
    raise TraceBackfillError("could not serialize unsupported mapper step")


def _step_from_wire_payload(payload: Any) -> Step:
    if not isinstance(payload, dict):
        raise TraceBackfillError("redacted mapper payload is not an object")
    source = payload.get("source")
    if source == "user":
        return Step.model_validate(payload)
    if source == "agent":
        return AgentStep.model_validate(payload)
    if source == "tool":
        return ToolStep.model_validate(payload)
    raise TraceBackfillError("redacted mapper payload has an unsupported source")


def _validate_step(step: Step) -> None:
    if not step.started_at or not step.ended_at:
        raise TraceBackfillError("historical mapper steps must carry deterministic start and end timestamps")
    started = _parse_timestamp(step.started_at)
    ended = _parse_timestamp(step.ended_at)
    if ended < started:
        raise TraceBackfillError("historical mapper step ended before it started")
    if step.source == "agent":
        if not isinstance(step, AgentStep):
            raise TraceBackfillError("agent-sourced mapper values must be AgentStep objects")
        visible = bool((step.content or "").strip() or (step.reasoning or "").strip() or step.tool_calls)
        if not visible:
            raise TraceBackfillError("mapper produced a blank HUD agent message")
        if any(call.arguments is not None and not isinstance(call.arguments, dict) for call in step.tool_calls):
            raise TraceBackfillError("mapper agent tool arguments must be parsed JSON objects")
    elif step.source == "tool":
        if not isinstance(step, ToolStep) or step.call is None or step.result is None:
            raise TraceBackfillError("tool-sourced mapper values must be complete ToolStep objects")
        if not step.call.name:
            raise TraceBackfillError("mapper produced a tool step with no tool name")
        if step.call.arguments is not None and not isinstance(step.call.arguments, dict):
            raise TraceBackfillError("mapper tool arguments must be parsed JSON objects")
    elif step.source == "user" and not _has_visible_user_text(step):
        raise TraceBackfillError("mapper produced a blank HUD user message")


def _validate_topology(items: Sequence[KeyedStep]) -> None:
    if not items or items[0].step.source != "user":
        raise TraceBackfillError("historical transcript must begin with exactly one user step")
    if sum(item.step.source == "user" for item in items) != 1:
        raise TraceBackfillError("historical transcript must contain exactly one first user step")
    pending: dict[str, MCPToolCall] = {}
    seen_calls: set[str] = set()
    terminal_seen = False
    for index, item in enumerate(items):
        step = item.step
        if isinstance(step, AgentStep):
            if pending:
                raise TraceBackfillError("assistant turn arrived with pending tool results")
            if step.done:
                if terminal_seen or index != len(items) - 1 or step.tool_calls:
                    raise TraceBackfillError("terminal done assistant must be unique, last, and have no tool calls")
                terminal_seen = True
            for call in step.tool_calls:
                if call.id in seen_calls:
                    raise TraceBackfillError("historical transcript contains a duplicate tool call id")
                seen_calls.add(call.id)
                pending[call.id] = call
        elif isinstance(step, ToolStep):
            if step.call is None or step.result is None or step.call.id not in pending:
                raise TraceBackfillError("historical transcript contains an orphan tool result")
            expected = pending.pop(step.call.id)
            if step.call.name != expected.name or _canonical_json(step.call.arguments) != _canonical_json(
                expected.arguments
            ):
                raise TraceBackfillError("tool result call does not match its prior assistant call")
        elif step.source != "user":
            raise TraceBackfillError("historical transcript contains an unsupported step source")
    if pending:
        raise TraceBackfillError("historical transcript ends with pending tool results")
    if not terminal_seen or not isinstance(items[-1].step, AgentStep):
        raise TraceBackfillError("historical transcript must end with a terminal done assistant")


def _strictly_monotonic_steps(items: Sequence[KeyedStep]) -> list[KeyedStep]:
    normalized: list[KeyedStep] = []
    previous_end: datetime | None = None
    tick = timedelta(microseconds=1)
    for item in items:
        started = _parse_timestamp(cast("str", item.step.started_at))
        ended = _parse_timestamp(cast("str", item.step.ended_at))
        try:
            if previous_end is not None and started <= previous_end:
                started = previous_end + tick
            if ended <= started:
                ended = started + tick
        except OverflowError:
            raise TraceBackfillError("historical mapper timestamps cannot be normalized safely") from None
        step = item.step.model_copy(
            update={
                "started_at": started.isoformat(timespec="microseconds"),
                "ended_at": ended.isoformat(timespec="microseconds"),
            }
        )
        normalized.append(KeyedStep(item.key, cast("Step", step)))
        previous_end = ended
    return normalized


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TraceBackfillError("historical mapper timestamp is not ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TraceBackfillError("historical mapper timestamps must include an offset")
    return parsed.astimezone(UTC)


def _has_visible_user_text(step: Step) -> bool:
    return bool(_visible_user_text(step))


def _visible_user_text(step: Step) -> str:
    payload = step.model_dump(mode="json", exclude_none=True)
    parts: list[str] = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n\n".join(parts)


def _expectation(step: Step, span_id: str) -> EventExpectation:
    kinds = {
        "user": "user_message",
        "agent": "agent_message",
        "tool": "tool_call",
        "task": "scenario_setup" if not step.task_call or step.task_call.phase == "setup" else "scenario_evaluate",
        "subagent": "subagent",
        "system": "raw",
    }
    tool_name = step.call.name if isinstance(step, ToolStep) and step.call else None
    return EventExpectation(
        span_id=span_id,
        kind=kinds[step.source],
        agent_must_be_visible=step.source == "agent",
        tool_name=tool_name,
        projection_sha256=(
            hashlib.sha256(_canonical_json(_projection_view_from_step(step))).hexdigest()
            if step.source in {"user", "agent", "tool"}
            else None
        ),
    )


def _projection_view_from_step(step: Step) -> dict[str, Any]:
    if step.source == "user":
        return {"kind": "user_message", "text": _visible_user_text(step)}
    if isinstance(step, AgentStep):
        return {
            "kind": "agent_message",
            "text": step.content,
            "reasoning": step.reasoning,
            "tool_calls": [
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments or {},
                }
                for call in step.tool_calls
            ],
        }
    if isinstance(step, ToolStep) and step.call is not None and step.result is not None:
        result = step.result.model_dump(mode="json", exclude_none=True)
        result_text, result_data = _normalize_result(result)
        is_error = bool(result.get("isError"))
        return {
            "kind": "tool_call",
            "tool_name": step.call.name,
            "arguments": step.call.arguments or {},
            "result_text": None if is_error else result_text,
            "result_data": result_data,
            "error": step.error or result_text or "tool error (no message)" if is_error or step.error else None,
        }
    raise TraceBackfillError("could not derive a projected view from mapper step")


def _projection_view_from_event(kind: str, event: Mapping[str, Any]) -> dict[str, Any]:
    if kind == "user_message":
        return {"kind": kind, "text": event.get("text")}
    if kind == "agent_message":
        calls: list[dict[str, Any]] = []
        for item in event.get("tool_calls", []):
            if not isinstance(item, dict):
                continue
            calls.append(
                {
                    "tool_call_id": item.get("tool_call_id"),
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or {},
                }
            )
        return {
            "kind": kind,
            "text": event.get("text"),
            "reasoning": event.get("reasoning"),
            "tool_calls": calls,
        }
    if kind == "tool_call":
        return {
            "kind": kind,
            "tool_name": event.get("tool_name"),
            "arguments": event.get("arguments") or {},
            "result_text": event.get("result_text"),
            "result_data": event.get("result_data"),
            "error": event.get("error"),
        }
    return {"kind": kind}


def _normalize_result(result: Any) -> tuple[str | None, Any | None]:
    if not isinstance(result, dict):
        return (result, None) if isinstance(result, str) else (None, result)
    if "structuredContent" in result:
        return None, result
    content = result.get("content")
    if isinstance(content, list):
        texts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        joined = "\n".join(texts) if texts else None
        return joined, result if joined is None else None
    if isinstance(content, str):
        return content, None
    return None, result


def _batches(spans: Sequence[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    size = 0
    for span in spans:
        encoded_size = len(_canonical_json(span))
        if encoded_size > _MAX_BATCH_BYTES:
            raise TraceBackfillError("one mapped HUD span exceeds the 4 MiB upload batch limit")
        if batch and (len(batch) >= _MAX_BATCH_SPANS or size + encoded_size > _MAX_BATCH_BYTES):
            yield batch
            batch = []
            size = 0
        batch.append(span)
        size += encoded_size
    if batch:
        yield batch


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _reject_symlink_components(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise TraceBackfillError("backfill ledger path may not traverse symlinks")
        current = current.parent


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _versioned_api_root(value: str) -> str:
    root = _safe_hud_root(value, label="HUD API URL", approved_hosts=_APPROVED_API_HOSTS)
    parsed = urlsplit(root)
    path = parsed.path
    if not path.endswith("/v2"):
        path += "/v2"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _safe_hud_root(value: str, *, label: str, approved_hosts: frozenset[str]) -> str:
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise TraceBackfillError(f"{label} contains an invalid port") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.hostname not in approved_hosts
    ):
        raise TraceBackfillError(f"{label} must use an approved HTTPS HUD origin without credentials or ports")
    if parsed.query or parsed.fragment:
        raise TraceBackfillError(f"{label} may not contain a query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _load_mapper(reference: str) -> StepMapper:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise TraceBackfillError("mapper must use the import form package.module:callable")
    try:
        module: ModuleType = importlib.import_module(module_name)
    except Exception as exc:
        raise TraceBackfillError(f"could not import mapper module ({type(exc).__name__})") from None
    mapper = getattr(module, attribute_name, None)
    if not callable(mapper):
        raise TraceBackfillError("mapper reference does not resolve to a callable")
    return cast("StepMapper", mapper)


def _assert_expected_counts(plan: BackfillPlan, args: argparse.Namespace) -> None:
    expected = {
        "agent_message": args.expect_agent,
        "tool_call": args.expect_tool,
        "user_message": args.expect_user,
    }
    drift = {
        kind: {"expected": count, "observed": plan.event_counts.get(kind, 0)}
        for kind, count in expected.items()
        if count is not None and plan.event_counts.get(kind, 0) != count
    }
    if drift:
        raise TraceBackfillError(f"mapper event counts do not match operator expectations: {drift}")


def _assert_apply_count_gate(args: argparse.Namespace) -> None:
    values = (args.expect_user, args.expect_agent, args.expect_tool)
    if args.apply and any(value is None for value in values):
        raise TraceBackfillError(
            "--apply requires --expect-user, --expect-agent, and --expect-tool"
        )
    if any(value is not None and value < 0 for value in values):
        raise TraceBackfillError("operator expected event counts may not be negative")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run or apply a deterministic historical HUD trace backfill")
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--mapper", required=True, help="Pure mapper import as package.module:callable")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Map, redact, and count locally without network access (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform terminal-trace telemetry upload; default is dry-run",
    )
    parser.add_argument("--ledger", type=Path, help="Required with --apply; durable mode-0600 JSON ledger")
    parser.add_argument("--expect-agent", type=int)
    parser.add_argument("--expect-tool", type=int)
    parser.add_argument("--expect-user", type=int)
    parser.add_argument("--verify-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--api-url", default=settings.hud_api_url)
    parser.add_argument("--telemetry-url", default=settings.hud_telemetry_url)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _assert_apply_count_gate(args)
        if not args.source.exists() or args.source.is_symlink():
            raise TraceBackfillError("mapper source must exist and may not be a symlink")
        mapper = _load_mapper(args.mapper)
        try:
            mapped = mapper(args.source.resolve())
        except Exception as exc:
            raise TraceBackfillError(f"source mapper failed ({type(exc).__name__})") from None
        plan = build_backfill_plan(
            args.trace_id,
            mapped,
            namespace=args.namespace,
        )
        _assert_expected_counts(plan, args)
        if not args.apply:
            print(json.dumps(plan.public_summary(mode="dry-run"), indent=2, sort_keys=True))
            return 0
        if args.ledger is None:
            raise TraceBackfillError("--ledger is required with --apply")
        api_key = os.environ.get("HUD_API_KEY", "")
        transport = HTTPBackfillTransport(
            api_url=args.api_url,
            telemetry_url=args.telemetry_url,
            api_key=api_key,
        )
        try:
            result = apply_backfill(
                plan,
                ledger=BackfillLedger(args.ledger),
                transport=transport,
                verify_timeout_seconds=args.verify_timeout,
                poll_interval_seconds=args.poll_interval,
            )
        finally:
            transport.close()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except TraceBackfillError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKFILL_SCHEMA_VERSION",
    "DEFAULT_NAMESPACE",
    "BackfillLedger",
    "BackfillPlan",
    "BackfillTransport",
    "HTTPBackfillTransport",
    "KeyedStep",
    "StepMapper",
    "StepRedactor",
    "TraceBackfillError",
    "apply_backfill",
    "build_backfill_plan",
    "deterministic_span_id",
    "main",
]

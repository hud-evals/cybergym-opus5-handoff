"""Typed binding and receipt exchanged by the HUD scheduler environment."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


def normalize_server(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("CyberGym server must be a private plain-http URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("CyberGym server URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class NativeTaskBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    task_id: str = Field(pattern=r"^(arvo|oss-fuzz):[^:]+$")
    server: str

    @model_validator(mode="after")
    def _normalize_server(self) -> NativeTaskBinding:
        self.server = normalize_server(self.server)
        return self


class NativeRunProfile(BaseModel):
    """Non-secret upstream runner settings attached to every HUD receipt."""

    model_config = ConfigDict(extra="forbid")

    budget_profile: Literal["paper-eval-100", "script-default-10", "custom"]
    model: str = Field(min_length=1)
    reasoning_effort: Literal["xhigh"] | None = None
    reasoning_transport: Literal["none", "gpt56_openai_responses_bridge"] = "none"
    response_storage: Literal["none", "openai_store_true"] = "none"
    response_continuation: Literal["none", "per_llm_previous_response_id_exact_transcript_extensions"] = "none"
    omitted_sampling_parameters: tuple[Literal["temperature", "top_p", "stop"], ...] = ()
    max_iter: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    temperature: float
    top_p: float
    seed: int | None = None
    native_tool_calling: bool | None = None
    base_url_mode: Literal["provider-default", "custom"]
    grader_server_mode: Literal["images", "binary"] = "images"
    runtime_nano_cpus: int | None = Field(default=None, gt=0)
    runtime_memory_bytes: int | None = Field(default=None, gt=0)
    runtime_memory_swap_bytes: int | None = Field(default=None, gt=0)
    network_mode: Literal["upstream-openhands-native-docker-default"] = "upstream-openhands-native-docker-default"
    upstream_commit: Literal["7656b71d07da6694e262f9c34ea994cd4849c0eb"] = "7656b71d07da6694e262f9c34ea994cd4849c0eb"
    agent_commit: Literal["b5cbe061b25e5719d296711706710438f6693079"] = "b5cbe061b25e5719d296711706710438f6693079"


class NativeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    runner: Literal["upstream-openhands-0.33-native"] = "upstream-openhands-0.33-native"
    status: Literal["completed", "error"]
    task_id: str = Field(pattern=r"^(arvo|oss-fuzz):[^:]+$")
    server: str
    run_profile: NativeRunProfile
    agent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    upstream_returned_agent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    log_dir: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> NativeReceipt:
        self.server = normalize_server(self.server)
        if self.status == "completed":
            if not self.agent_id or self.upstream_returned_agent_id != self.agent_id:
                raise ValueError("completed receipt requires the matching upstream agent ID")
            if self.error is not None:
                raise ValueError("completed receipt cannot contain an error")
        elif not self.error:
            raise ValueError("error receipt requires a diagnostic")
        return self

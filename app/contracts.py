"""HTTP request contracts owned by the application layer.

Routes parse transport input here and hand normalized values to the domain
layer.  This prevents frontend/API drift without moving orchestration logic
into ``app.routes``.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from agent.contracts import (
    normalize_reasoning_effort,
)


class ContractValidationError(ValueError):
    """A client request did not satisfy a public API contract."""


async def read_json_object(request: Request) -> dict[str, Any]:
    """Read one JSON object and preserve the application's 400 error contract."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return body


def _required_string(value: Any, field: str, *, limit: int | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value[:limit] if limit is not None else value


def _optional_text(value: Any, field: str, limit: int) -> str | None:
    if value in (None, ""):
        return None
    text = _required_string(value, field, limit=limit).strip()
    return text or None


def _optional_id(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


class ChatSendRequest(BaseModel):
    """Public contract for ``POST /api/chat/send``.

    ``reasoning_effort`` is the only request-level model control.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str | None = None
    project_id: str | None = None
    message: str = ""
    client_request_id: str | None = None
    images: list[str] | None = None
    regenerate_message_id: int | None = None
    edit_message_id: int | None = None
    location: str | None = None
    handoff_path: str | None = None
    reasoning_effort: str | None = None

    @field_validator("session_id", mode="before")
    @classmethod
    def _validate_session_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _required_string(value, "session_id")

    @field_validator("project_id", mode="before")
    @classmethod
    def _validate_project_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return _required_string(value, "project_id")

    @field_validator("message", mode="before")
    @classmethod
    def _validate_message(cls, value: Any) -> str:
        return _required_string(value, "message")

    @field_validator("client_request_id", mode="before")
    @classmethod
    def _validate_request_id(cls, value: Any) -> str | None:
        return _optional_text(value, "client_request_id", 120)

    @field_validator("images", mode="before")
    @classmethod
    def _validate_images(cls, value: Any) -> list[str] | None:
        if value in (None, []):
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("images must be a list of strings")
        return value

    @field_validator("regenerate_message_id", mode="before")
    @classmethod
    def _validate_regenerate_message_id(cls, value: Any) -> int | None:
        return _optional_id(value, "regenerate_message_id")

    @field_validator("edit_message_id", mode="before")
    @classmethod
    def _validate_edit_message_id(cls, value: Any) -> int | None:
        return _optional_id(value, "edit_message_id")

    @field_validator("location", mode="before")
    @classmethod
    def _validate_location(cls, value: Any) -> str | None:
        return _optional_text(value, "location", 60)

    @field_validator("handoff_path", mode="before")
    @classmethod
    def _validate_handoff_path(cls, value: Any) -> str | None:
        return _optional_text(value, "handoff_path", 240)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = normalize_reasoning_effort(value)
        if normalized != value:
            raise ValueError("reasoning_effort must be off, low, high, or max")
        return normalized

    @model_validator(mode="after")
    def _fill_reasoning_effort(self) -> "ChatSendRequest":
        if self.reasoning_effort is None:
            self.reasoning_effort = "high"
        return self


def parse_chat_send(body: Any) -> ChatSendRequest:
    """Return a normalized chat request or a route-safe validation error."""
    if not isinstance(body, dict):
        raise ContractValidationError("request body must be an object")
    try:
        return ChatSendRequest.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        message = first.get("msg") or "invalid request"
        raise ContractValidationError(message) from exc

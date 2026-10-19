"""Validated model-to-coordinator protocol. No model response selects a write path."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Message):
    path: str = Field(min_length=1, max_length=2000)
    symbol: str = Field(max_length=500)
    revision: str = Field(min_length=1, max_length=200)
    observation: str = Field(min_length=1, max_length=4000)


class Research(Message):
    summary: str = Field(min_length=1, max_length=12000)
    requirements: list[str] = Field(min_length=1, max_length=100)
    scenarios: list[str] = Field(min_length=1, max_length=100)
    evidence: list[Evidence] = Field(min_length=1, max_length=200)
    questions: list[str] = Field(max_length=20)


class Draft(Message):
    jira: str = Field(min_length=20, max_length=100000)
    evidence: list[Evidence] = Field(min_length=1, max_length=200)
    questions: list[str] = Field(max_length=20)
    change_log: list[str] = Field(max_length=100)


class Finding(Message):
    id: str = Field(min_length=1, max_length=100)
    severity: Literal["blocker", "major", "minor"]
    blocking: bool
    claim: str = Field(min_length=1, max_length=4000)
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    impact: str = Field(min_length=1, max_length=4000)
    requested_change: str = Field(min_length=1, max_length=8000)
    status: Literal["open", "closed", "disputed"]
    draft_anchor: str = Field(min_length=1, max_length=2000)


class Review(Message):
    reviewed_version: int = Field(ge=1)
    findings: list[Finding] = Field(max_length=200)
    questions: list[str] = Field(max_length=20)


def unresolved(review: Review) -> list[Finding]:
    return [f for f in review.findings if f.status != "closed"
            and (f.blocking or f.severity in {"blocker", "major"})]

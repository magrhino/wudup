"""Shared release-note records and type aliases.

These existing dataclasses are re-exported by release_notes. A single definition
keeps providers, advisory assessment, and cache conversion independent of the
public orchestration module."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .lsio_updates import LSIOUpdateClassification
from .wud_file import WudTarget

ReleaseNoteStatus = Literal[
    "cached",
    "ready",
    "missing",
    "unsupported",
    "not_found",
    "error",
]
ReleaseSecurityOutcome = Literal[
    "verified_critical_high",
    "needs_review",
    "ordinary",
]
ReleaseSecuritySeverity = Literal[
    "critical",
    "high",
    "moderate",
    "low",
    "unknown",
    "none",
]


@dataclass(frozen=True)
class ReleaseNoteLink:
    label: str
    url: str
    kind: str


@dataclass(frozen=True)
class ReleaseSecurityAssessment:
    outcome: ReleaseSecurityOutcome = "ordinary"
    severity: ReleaseSecuritySeverity = "none"
    reason_code: str = "no_security_signal"
    reason: str = "No security urgency signal was found in the release notes."
    advisory_ids: list[str] = field(default_factory=list)
    lookup_truncated: bool = False


@dataclass(frozen=True)
class ReleaseNoteInfo:
    line_no: int
    status: ReleaseNoteStatus
    provider: str
    image_repo: str
    upstream_repo: str
    release_tag: str = ""
    title: str = ""
    published_at: str = ""
    breaking: bool = False
    breaking_reasons: list[str] = field(default_factory=list)
    links: list[ReleaseNoteLink] = field(default_factory=list)
    refreshed_at: str = ""
    error: str = ""
    body: str = ""
    classification: LSIOUpdateClassification = field(
        default_factory=LSIOUpdateClassification
    )
    security: ReleaseSecurityAssessment = field(
        default_factory=ReleaseSecurityAssessment
    )


@dataclass(frozen=True)
class ReleaseNoteContext:
    line_no: int
    cache_key: str
    provider: str
    image_repo: str
    upstream_repo: str
    current_tag: str
    target_tag: str
    target_digest: str = ""
    error: str = ""


@dataclass(frozen=True)
class GitHubLatestCandidate:
    release_tag: str
    link_label: str
    link_url: str


ReleaseNoteSourceResolver = Callable[[WudTarget], str]
ReleaseNoteTargetTagResolver = Callable[[WudTarget], str]

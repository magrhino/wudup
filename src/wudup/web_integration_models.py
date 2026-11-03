"""Response models for the read-only integrations API (`/api/integrations/v1`).

The shapes are a versioned contract for machine consumers. Within ``v: 1``
only add optional fields; renaming or removing a field needs a new version.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .web_models import WudApiState

INTEGRATIONS_API_VERSION = 1

IntegrationSecurity = Literal["critical_high", "review", "none", "unknown"]
IntegrationIssueSeverity = Literal["urgent", "digest"]
IntegrationRunStatus = Literal[
    "success", "failure", "missed", "needs_review", "running", "unknown"
]


class IntegrationWudStatus(BaseModel):
    state: WudApiState
    fresh: bool
    detail: str = ""


class IntegrationSeverityCounts(BaseModel):
    critical: int = 0
    high: int = 0


class IntegrationUpdate(BaseModel):
    # `stack/service`, the stack alone for a stack-level match, or "" when the
    # update matched no Compose stack. Image repos never appear here: they
    # contain a slash and could be mistaken for a service key.
    service: str
    image: str = ""
    current: str
    target: str
    kind: str = ""
    breaking: bool = False
    security: IntegrationSecurity = "unknown"
    fixes: IntegrationSeverityCounts | None = None
    scan: str = "unknown"
    auto: bool = False
    skipped: bool = False


class IntegrationUpdates(BaseModel):
    total: int = 0
    items: list[IntegrationUpdate] = Field(default_factory=list)
    more: int = 0
    unplaced: int = 0
    snoozed: int = 0


class IntegrationRun(BaseModel):
    id: int
    at: str
    mode: str
    status: IntegrationRunStatus
    services: int = 0
    failed: list[str] = Field(default_factory=list)
    reason: str = ""


class IntegrationIssue(BaseModel):
    id: str
    severity: IntegrationIssueSeverity
    code: str
    name: str
    summary: str
    since: str = ""


class IntegrationSummaryResponse(BaseModel):
    v: Literal[1] = INTEGRATIONS_API_VERSION
    checked_at: str
    wud: IntegrationWudStatus
    updates: IntegrationUpdates
    last_run: IntegrationRun | None = None
    issues: list[IntegrationIssue] = Field(default_factory=list)
    issues_more: int = 0


class IntegrationRelease(BaseModel):
    tag: str = ""
    title: str = ""
    published_at: str = ""
    breaking: bool = False
    breaking_reasons: list[str] = Field(default_factory=list)
    security: IntegrationSecurity = "unknown"
    advisories: list[str] = Field(default_factory=list)
    link: str = ""


class IntegrationScan(BaseModel):
    state: str
    scanned_at: str = ""
    comparison: str = "unknown"
    target: IntegrationSeverityCounts = Field(default_factory=IntegrationSeverityCounts)
    fixes: IntegrationSeverityCounts | None = None


class IntegrationPolicy(BaseModel):
    mode: str = ""
    auto: bool = False
    time: str = ""
    days: list[str] = Field(default_factory=list)


class IntegrationSnooze(BaseModel):
    kind: str
    until: str = ""
    label: str = ""
    reason: str = ""


class IntegrationServiceResponse(BaseModel):
    v: Literal[1] = INTEGRATIONS_API_VERSION
    service: str
    image: str = ""
    current: str = ""
    target: str = ""
    kind: str = ""
    runtime: str = "unknown"
    update_available: bool = False
    release: IntegrationRelease | None = None
    scan: IntegrationScan | None = None
    policy: IntegrationPolicy | None = None
    snooze: IntegrationSnooze | None = None
    last_run: IntegrationRun | None = None

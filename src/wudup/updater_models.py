"""Data models and exceptions for the updater."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .command import CommandError, CommandResult
from .compose import ComposeStack
from .config import DEFAULT_MAX_WAIT, DEFAULT_UPDATE_MODE
from .wud_file import WudTarget

STALE_PENDING_DIGEST_REASON = "stale-pending-digest"
DigestRequirementKey = tuple[int, int, str]


@dataclass
class DigestRequirementOutcomes:
    stale: set[DigestRequirementKey] = field(default_factory=set)
    failed: set[DigestRequirementKey] = field(default_factory=set)
    viable: set[DigestRequirementKey] = field(default_factory=set)

    def outcome(self, key: DigestRequirementKey) -> str:
        if key in self.stale:
            return "stale"
        if key in self.failed:
            return "failed"
        if key in self.viable:
            return "viable"
        return ""

    def failed_in_stack(self, stack_index: int) -> bool:
        return any(key[0] == stack_index for key in self.failed)


class UpdaterError(RuntimeError):
    """Raised for a user-facing updater failure."""


class ComposeTagRewriteError(RuntimeError):
    """Raised when a Compose tag rewrite cannot be proven safe."""


class ResolvedTagMarkerConflictError(ComposeTagRewriteError):
    def __init__(self, *, service: str, tags: tuple[str, ...]) -> None:
        self.service = service
        self.tags = tags
        super().__init__(
            f"Service {service} has conflicting resolved-tag markers: "
            f"{', '.join(tags)}."
        )


@dataclass(frozen=True)
class UpdaterOptions:
    docker_base: Path
    wud_file: Path
    log_dir: Path
    mode: str = DEFAULT_UPDATE_MODE
    max_wait: int = DEFAULT_MAX_WAIT
    dry_run: bool = False
    assume_yes: bool = False
    allow_tag_updates: bool = False
    digest_pin_updates: bool = False
    no_color: bool = False
    only_lines: str = ""
    remove_lines_before_run: str = ""
    tag_overrides: tuple[TagOverride, ...] = ()
    exclude_tag_lines: str = ""
    recreate_excluded_services: bool = False
    compose_ignore_paths: tuple[Path, ...] = ()
    db_path: Path | None = None
    docker_base_label: str | None = None
    host_docker_base: Path | None = None
    host_docker_base_label: str | None = None
    wud_file_label: str | None = None
    log_dir_label: str | None = None
    metadata_json: str = "{}"
    digest_pin_plan: tuple[DigestPinUpdate, ...] = ()
    digest_unpin_plan: tuple[DigestUnpinUpdate, ...] = ()
    digest_pin_label_rewrite_approvals: tuple[
        DigestPinLabelRewriteApproval, ...
    ] = ()
    update_selections: tuple[UpdateSelection, ...] = ()
    completed_update_selections: tuple[CompletedUpdateSelection, ...] = ()
    tag_stream_updates: tuple[TagStreamUpdate, ...] = ()
    # None disables WebUI self-protection; empty enables image checks without a runtime target.
    protected_container: str | None = None


@dataclass(frozen=True)
class UpdateSelection:
    line_no: int
    selection_id: str = ""


@dataclass(frozen=True)
class CompletedUpdateSelection:
    target_key: str
    completion_id: str


@dataclass(frozen=True)
class Match:
    stack: ComposeStack
    target: WudTarget
    resolved: str
    compose_image: str
    service: str


@dataclass(frozen=True)
class ImageState:
    image_id: str
    digest: str


@dataclass(frozen=True)
class TagUpdate:
    old_image: str
    desired_tag: str
    new_image: str
    services: tuple[str, ...]


@dataclass(frozen=True)
class TagStreamDecision:
    line_no: int
    decision: str


@dataclass(frozen=True)
class TagStreamLabelRewriteApproval:
    line_no: int
    stack: str
    stack_directory: str
    compose_file: str
    service: str
    label_key: str
    current_label_value: str
    selected_tag: str
    proposed_label_value: str


@dataclass(frozen=True)
class TagStreamUpdate:
    line_no: int
    stack: str
    stack_directory: str
    compose_file: str
    service: str
    current_tag: str
    reported_tag: str
    selected_tag: str
    decision: str
    label_key: str
    current_label_value: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str


@dataclass(frozen=True)
class DigestPinUpdate:
    old_image: str
    resolved_tag: str
    resolved_image: str
    planned_digest: str
    final_image: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    services: tuple[str, ...]


@dataclass(frozen=True)
class DigestUnpinUpdate:
    old_image: str
    resolved_tag: str
    tag_image: str
    current_digest: str
    target_digest: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    services: tuple[str, ...]


@dataclass(frozen=True)
class DigestPinCandidate:
    old_image: str
    resolved_tag: str
    resolved_image: str
    planned_digest: str
    services: tuple[str, ...]


@dataclass(frozen=True)
class DigestPinLabelRewriteApproval:
    stack: str
    service: str
    label_key: str
    current_label_value: str
    planned_tag: str
    proposed_label_value: str


@dataclass(frozen=True)
class DigestPinLabelRewrite:
    service: str
    label_key: str
    current_label_value: str
    planned_tag: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str


@dataclass(frozen=True)
class TagOverride:
    line_no: int
    tag: str


@dataclass(frozen=True)
class TagExclusionUpdate:
    stack: ComposeStack
    service: str
    image: str
    image_repo: str
    tag: str
    source_line: int
    scope: str

    @property
    def service_key(self) -> str:
        return f"{self.stack.name}/{self.service}"


@dataclass(frozen=True)
class AppliedTagExclusion:
    service: str
    image_repo: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class AppliedTagUpdate(TagUpdate):
    replacements: int


@dataclass(frozen=True)
class AppliedDigestPinUpdate(DigestPinUpdate):
    replacements: int
    label_rewrites: tuple[DigestPinLabelRewrite, ...] = ()


@dataclass(frozen=True)
class AppliedDigestUnpinUpdate(DigestUnpinUpdate):
    replacements: int


class DigestPinLabelRewriteApprovalRequired(ComposeTagRewriteError):
    def __init__(
        self,
        *,
        service: str,
        label_key: str,
        current_label_value: str,
        planned_tag: str,
        proposed_label_value: str,
        proposed_label_regex: str,
    ) -> None:
        self.service = service
        self.label_key = label_key
        self.current_label_value = current_label_value
        self.planned_tag = planned_tag
        self.proposed_label_value = proposed_label_value
        self.proposed_label_regex = proposed_label_regex
        super().__init__(
            f'Service {service} {label_key} is "{current_label_value}"; this may '
            "be a custom regex or non-matching tag and needs approval before "
            f'replacing it with "{proposed_label_regex}".'
        )


@dataclass(frozen=True)
class StackStatus:
    status: str
    reason: str


@dataclass(frozen=True)
class UpResult:
    ok: bool
    wait_handled: bool
    command_error: CommandError | None = None
    health_details: str = ""


@dataclass(frozen=True)
class UpdateScope:
    services: tuple[str, ...] | None
    pull_services: tuple[str, ...] | None
    stack_reason: str = ""
    stop_services: tuple[str, ...] | None = None
    force_recreate: bool = False
    up_no_deps: bool = True


@dataclass
class FailureRecord:
    stack: ComposeStack
    services: tuple[str, ...] | None
    matches: tuple[Match, ...]
    phase: str
    reason: str
    command_result: CommandResult | None = None
    health_details: str = ""
    note: str = ""
    wud_restored: bool | None = None


@dataclass(frozen=True)
class UpdaterProgressEvent:
    phase: str
    status: str
    message: str
    stack: str = ""
    services: tuple[str, ...] = ()
    line_numbers: tuple[int, ...] = ()

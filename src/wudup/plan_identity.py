"""Stable identity helpers for WebUI dry-run plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .config import UpdaterConfig
from .plan_models import DryRunPlan, PlanFileMissing
from .updater_models import (
    DigestPinLabelRewriteApproval,
    TagOverride,
    TagStreamDecision,
    TagStreamLabelRewriteApproval,
)


def _file_sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise PlanFileMissing(f"WUD file not found: {path}") from exc
    return hashlib.sha256(data).hexdigest()


def _plan_id(
    plan: DryRunPlan,
    *,
    config: UpdaterConfig,
    allow_tag_updates: bool,
    tag_overrides: Sequence[TagOverride],
    tag_stream_decisions: Sequence[TagStreamDecision],
    tag_stream_label_rewrite_approvals: Sequence[TagStreamLabelRewriteApproval],
    digest_pin_label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval],
    host_docker_base: Path | None,
    wud_file_hash: str,
    source_file: str | None = None,
) -> str:
    plan_payload = asdict(plan)
    plan_payload.pop("plan_id", None)
    plan_payload.pop("can_apply", None)
    source_payload = plan_payload.get("source")
    if isinstance(source_payload, dict):
        for key in ("fresh", "degraded", "fallback_reason", "detail"):
            source_payload.pop(key, None)
        for key in ("source_ids_by_line", "metadata_status_by_line"):
            source_payload[key] = {
                line_no: value
                for line_no, value in source_payload.get(key, {}).items()
                if line_no in plan.selected_line_numbers
            }
    payload = {
        "version": 1,
        "allow_tag_updates": allow_tag_updates,
        "tag_overrides": [
            {"line_no": item.line_no, "tag": item.tag}
            for item in sorted(tag_overrides, key=lambda item: item.line_no)
        ],
        "tag_stream_decisions": [
            {"line_no": item.line_no, "decision": item.decision}
            for item in sorted(
                tag_stream_decisions,
                key=lambda item: (item.line_no, item.decision),
            )
        ],
        "tag_stream_label_rewrite_approvals": [
            {
                "line_no": item.line_no,
                "stack": item.stack,
                "stack_directory": item.stack_directory,
                "compose_file": item.compose_file,
                "service": item.service,
                "label_key": item.label_key,
                "current_label_value": item.current_label_value,
                "selected_tag": item.selected_tag,
                "proposed_label_value": item.proposed_label_value,
            }
            for item in sorted(
                tag_stream_label_rewrite_approvals,
                key=lambda item: (
                    item.line_no,
                    item.stack,
                    item.stack_directory,
                    item.compose_file,
                    item.service,
                    item.label_key,
                    item.current_label_value,
                    item.selected_tag,
                    item.proposed_label_value,
                ),
            )
        ],
        "digest_pin_label_rewrite_approvals": [
            {
                "stack": item.stack,
                "service": item.service,
                "label_key": item.label_key,
                "current_label_value": item.current_label_value,
                "planned_tag": item.planned_tag,
                "proposed_label_value": item.proposed_label_value,
            }
            for item in sorted(
                digest_pin_label_rewrite_approvals,
                key=lambda item: (
                    item.stack,
                    item.service,
                    item.label_key,
                    item.current_label_value,
                    item.planned_tag,
                    item.proposed_label_value,
                ),
            )
        ],
        "docker_base": str(config.docker_base),
        "host_docker_base": "" if host_docker_base is None else str(host_docker_base),
        "max_wait": config.max_wait,
        "mode": config.update_mode,
        "plan": plan_payload,
        "source_file": source_file or str(config.wud_out_file),
        "wud_file_sha256": wud_file_hash,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

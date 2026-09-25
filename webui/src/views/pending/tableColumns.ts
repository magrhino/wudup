import { h, type VNodeChild } from "vue";
import { NInput, NTag, type DataTableColumns } from "naive-ui";

import type { PendingItem, ReleaseNoteInfo } from "../../api/client";
import PendingEvidenceExplanation from "../../components/pending/PendingEvidenceExplanation.vue";
import PendingReleaseNotes from "../../components/pending/PendingReleaseNotes.vue";
import { digestProvenanceDisplay } from "../../utils/digestProvenance";
import {
  pendingMetadataStatusLabel,
  pendingMetadataStatusTagType,
  pendingMetadataStatusTitle,
} from "./pendingDisplay";
import type { SafetyCue } from "./safetyCues";

export type PendingTableColumnsContext = {
  displayDigest: (value: string) => string;
  displayValue: (value: string) => string;
  releaseNoteFor: (item: PendingItem) => ReleaseNoteInfo | null;
  releaseNoteReason: (note: ReleaseNoteInfo | null) => string;
  releaseNoteStatus: (note: ReleaseNoteInfo | null) => string;
  riskCues: (row: PendingItem) => SafetyCue[];
  tagInputProps: (item: Pick<PendingItem, "image">) => { "aria-label": string };
  tagOverrideValue: (item: PendingItem) => string;
  updateTagOverride: (item: PendingItem, value: string) => void;
};

export function createPendingColumns(
  context: PendingTableColumnsContext,
): DataTableColumns<PendingItem> {
  return [
    { type: "selection", width: 48 },
    { title: "Line", key: "line_no", width: 80 },
    {
      title: "Image",
      key: "image",
      minWidth: 240,
      render: (row) =>
        h("code", { class: "pending-table-value", title: row.image }, row.image),
    },
    {
      title: "Repository",
      key: "repo",
      minWidth: 200,
      render: (row) =>
        h("span", { class: "pending-table-value", title: row.repo }, row.repo),
    },
    {
      title: "Current tag",
      key: "current_tag",
      minWidth: 120,
      render: (row) => context.displayValue(row.current_tag),
    },
    {
      title: "New tag",
      key: "desired_tag",
      minWidth: 160,
      render: (row) => {
        if (!row.desired_tag) {
          return context.displayValue("");
        }
        return h(NInput, {
          value: context.tagOverrideValue(row),
          size: "small",
          class: "tag-override-input",
          placeholder: row.desired_tag,
          inputProps: context.tagInputProps(row),
          onUpdateValue: (value: string) => context.updateTagOverride(row, value),
        });
      },
    },
    {
      title: "New digest",
      key: "digest",
      minWidth: 220,
      render: (row) => {
        const provenance = digestProvenanceDisplay(row.digest_provenance);
        if (provenance) {
          return h("div", { class: "digest-provenance", title: provenance.title }, [
            h("span", { class: "digest-provenance-primary" }, provenance.primary),
            provenance.digest
              ? h("code", { class: "digest-value" }, provenance.digest)
              : null,
          ]);
        }
        return row.digest
          ? h(
              "code",
              { class: "digest-value", title: row.digest },
              context.displayDigest(row.digest),
            )
          : context.displayValue("");
      },
    },
    {
      title: "Metadata",
      key: "metadata_status",
      minWidth: 120,
      render: (row) =>
        h(
          NTag,
          {
            size: "small",
            type: pendingMetadataStatusTagType(row),
            title: pendingMetadataStatusTitle(row),
          },
          () => pendingMetadataStatusLabel(row),
        ),
    },
    {
      title: "Safety cues",
      key: "safety_cues",
      minWidth: 200,
      render: (row) => renderRiskBadges(row, context.riskCues),
    },
    {
      title: "Release notes",
      key: "release_notes",
      minWidth: 220,
      render: (row) => renderReleaseNotes(row, context),
    },
  ];
}

export function renderRiskBadges(
  row: PendingItem,
  riskCues: (row: PendingItem) => SafetyCue[],
): VNodeChild {
  const cues = riskCues(row);
  const badges = cues.map((cue) =>
    h(
      NTag,
      { key: cue.key, size: "small", type: cue.type, class: "safety-badge" },
      () => cue.label,
    ),
  );
  if (badges.length === 0) {
    return h("span", { class: "risk-badges-muted" }, "None");
  }
  return h("div", { class: "risk-badges-container" }, [
    ...badges,
    h(PendingEvidenceExplanation, { cues }),
  ]);
}

export function renderReleaseNotes(
  row: PendingItem,
  context: Pick<
    PendingTableColumnsContext,
    "releaseNoteFor" | "releaseNoteReason" | "releaseNoteStatus"
  >,
): VNodeChild {
  const note = context.releaseNoteFor(row);
  return h(PendingReleaseNotes, {
    candidateLabel: `${row.image} → ${row.desired_tag || row.current_tag}`,
    candidateTag: row.desired_tag || row.current_tag,
    releaseNote: note,
    releaseNoteReason: context.releaseNoteReason(note),
    releaseNoteStatus: context.releaseNoteStatus(note),
  });
}

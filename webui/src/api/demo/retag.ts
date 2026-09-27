import type {
  RetagChoiceRequest,
  RetagPlanResponse,
  RetagTargetItem,
} from "../types";
import { STATIC_DEMO_READ_ONLY_MESSAGE } from "./constants";
import { demoIdPart } from "./helpers";

function retagTargetKey(item: RetagTargetItem): string {
  return item.target_id || item.service_key;
}

function retagChoiceKey(choice: RetagChoiceRequest): string {
  return choice.target_id || choice.service_key;
}

function lookupRetagChoiceTarget(
  choice: RetagChoiceRequest,
  targetById: Map<string, RetagTargetItem>,
  targetByUniqueService: Map<string, RetagTargetItem>,
): RetagTargetItem | undefined {
  if (choice.target_id) {
    return targetById.get(choice.target_id);
  }
  return targetByUniqueService.get(choice.service_key);
}

export function retagPlanFromChoices(
  targets: RetagTargetItem[],
  choices: RetagChoiceRequest[],
): RetagPlanResponse {
  const normalized = normalizedRetagChoices(targets, choices);
  const selected = normalized
    .map((choice) => ({
      choice,
      item: targets.find(
        (item) => retagTargetKey(item) === retagChoiceKey(choice),
      ),
    }))
    .filter(
      (
        entry,
      ): entry is { choice: RetagChoiceRequest; item: RetagTargetItem } =>
        Boolean(entry.item) && entry.choice.choice === "switch-to-concrete",
    );
  const issues = selected
    .filter(({ item, choice }) => !retagTargetTag(item, choice))
    .map(({ item }) => ({
      severity: "error",
      code: "missing-target-tag",
      message: `${item.service_key} needs a concrete target tag.`,
      service_key: item.service_key,
      stack: item.stack,
      service: item.service,
      hint: "Choose a concrete tag before applying the retag plan.",
      details: {},
    }));
  const updates = selected
    .filter(({ item, choice }) => retagTargetTag(item, choice))
    .map(({ item, choice }) => retagPlanUpdate(item, choice));
  const stacks = targets
    .map((item) => ({
      stack: item.stack,
      directory: item.directory,
      compose_file: item.compose_file,
      project_directory: item.project_directory,
    }))
    .filter(
      (stack, index, stacks) =>
        stacks.findIndex(
          (candidate) =>
            candidate.stack === stack.stack &&
            candidate.directory === stack.directory &&
            candidate.compose_file === stack.compose_file &&
            candidate.project_directory === stack.project_directory,
        ) === index,
    )
    .map((stack) => ({
      ...stack,
      services: updates
        .filter((update) => update.stack === stack.stack)
        .map((update) => update.service),
      tag_updates: updates.filter(
        (update) =>
          update.stack === stack.stack &&
          targets.some(
            (item) =>
              item.service_key === update.service_key &&
              item.directory === stack.directory &&
              item.compose_file === stack.compose_file &&
              item.project_directory === stack.project_directory,
          ),
      ),
      digest_pin_updates: [],
    }))
    .filter((stack) => stack.tag_updates.length > 0);
  const selectedCount = updates.length;
  let status: RetagPlanResponse["status"] = "empty";
  if (selectedCount > 0) {
    status = issues.length > 0 ? "blocked" : "ready";
  }
  return {
    plan_id:
      selectedCount === 0
        ? "demo-retag-empty"
        : `demo-retag-${updates
            .map((update) => `${demoIdPart(update.service_key)}-${demoIdPart(update.target_tag)}`)
            .join("-")}`,
    status,
    can_apply: false,
    external_recreate_required: true,
    selected_count: selectedCount,
    keep_current_count: normalized.length - selectedCount,
    stacks,
    issues,
    warnings: [
      STATIC_DEMO_READ_ONLY_MESSAGE,
    ],
  };
}

function retagPlanUpdate(
  item: RetagTargetItem,
  choice: RetagChoiceRequest,
): RetagPlanResponse["stacks"][number]["tag_updates"][number] {
  const tag = retagTargetTag(item, choice);
  const finalImage = retagFinalImage(item, tag);
  return {
    target_id: retagTargetKey(item),
    service_key: item.service_key,
    stack: item.stack,
    service: item.service,
    source_image: item.image,
    target_tag: tag,
    final_image: finalImage,
    label_key: item.label_key,
    label_value: item.label_value,
    label_rewrites: item.label_key
      ? [
          {
            service: item.service,
            label_key: item.label_key,
            current_label_value: item.label_value,
            planned_tag: tag,
            proposed_label_value: item.label_value
              ? item.label_value.replace(item.tracking_tag, tag)
              : tag,
            proposed_label_regex: "",
            approved: true,
            reason: "demo",
          },
        ]
      : [],
  };
}

function retagTargetTag(
  item: RetagTargetItem,
  choice: RetagChoiceRequest,
): string {
  const tag = choice.target_tag?.trim() || item.proposed_tag;
  return tag && tag !== "latest" ? tag : "";
}

function retagFinalImage(item: RetagTargetItem, tag: string): string {
  if (!tag) {
    return item.final_image;
  }
  if (item.final_image.includes(`:${item.proposed_tag}`)) {
    return item.final_image.replace(`:${item.proposed_tag}`, `:${tag}`);
  }
  return `${item.image_repo}:${tag}`;
}

function normalizedRetagChoices(
  targets: RetagTargetItem[],
  choices: RetagChoiceRequest[],
): RetagChoiceRequest[] {
  const serviceCounts = new Map<string, number>();
  for (const item of targets) {
    serviceCounts.set(item.service_key, (serviceCounts.get(item.service_key) ?? 0) + 1);
  }
  const targetById = new Map(
    targets.map((item) => [retagTargetKey(item), item]),
  );
  const targetByUniqueService = new Map(
    targets
      .filter((item) => serviceCounts.get(item.service_key) === 1)
      .map((item) => [item.service_key, item]),
  );
  const byTarget = new Map<string, RetagChoiceRequest>();
  for (const choice of choices) {
    const targetId = choice.target_id ?? "";
    if (!targetId && serviceCounts.get(choice.service_key) !== 1) {
      throw new Error(
        "retag choices for duplicate service(s) must include target_id: "
          + choice.service_key,
      );
    }
    const target = lookupRetagChoiceTarget(choice, targetById, targetByUniqueService);
    if (!target) {
      throw new Error(
        targetId
          ? "Static demo retag target was not found."
          : "Static demo retag service was not found.",
      );
    }
    if (target.service_key !== choice.service_key) {
      throw new Error("Static demo retag target does not match service_key.");
    }
    const key = retagTargetKey(target);
    if (byTarget.has(key)) {
      throw new Error("retag choices contain duplicate target(s): " + choice.service_key);
    }
    byTarget.set(key, choice);
  }
  return targets.map((item) => {
    const requested = byTarget.get(retagTargetKey(item));
    const choice = requested?.choice ?? "keep-current";
    const targetTag = requested?.target_tag?.trim() ?? "";
    const normalized: RetagChoiceRequest = {
      service_key: item.service_key,
      choice,
    };
    if (item.target_id) {
      normalized.target_id = item.target_id;
    }
    if (choice === "switch-to-concrete" && targetTag) {
      normalized.target_tag = targetTag;
    }
    if (choice === "switch-to-concrete" && requested?.allow_start) {
      normalized.allow_start = true;
    }
    return normalized;
  });
}

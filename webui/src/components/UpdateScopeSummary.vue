<script setup lang="ts">
import { computed } from "vue";
import { imageTransition, scopeTitle, type ImageChange } from "../utils/updateSummary";

const props = defineProps<{ changes: ImageChange[]; fallback: string; compact?: boolean }>();
const groups = computed(() => {
  const stacks = new Map<string, ImageChange[]>();
  for (const change of props.changes) {
    const name = change.stack || "Unknown stack";
    const items = stacks.get(name) ?? [];
    items.push(change);
    stacks.set(name, items);
  }
  return [...stacks];
});
</script>

<template>
  <div class="update-scope-summary" :class="{ compact }">
    <component :is="compact ? 'strong' : 'h2'" class="scope-title">{{ scopeTitle(changes, fallback) }}</component>
    <p v-if="!compact && changes.length === 1" class="scope-context">
      Stack {{ changes[0]!.stack || "not recorded" }}
    </p>
    <details v-if="!compact && changes.length" class="scope-changes">
      <summary>Inspect {{ changes.length === 1 ? 'image change' : `all ${changes.length} service changes` }}</summary>
      <section v-for="[stack, items] in groups" :key="stack" class="scope-stack">
        <h3>{{ stack }}</h3>
        <div v-for="(item, index) in items" :key="index" class="scope-change">
          <strong>{{ item.service || 'Service not recorded' }}</strong>
          <span>{{ imageTransition(item) }}</span>
          <dl>
            <div><dt>From</dt><dd><code>{{ item.before || 'Not recorded' }}</code></dd></div>
            <div><dt>To</dt><dd><code>{{ item.after || 'Not recorded' }}</code></dd></div>
          </dl>
        </div>
      </section>
    </details>
  </div>
</template>

<style scoped>
.update-scope-summary { min-width: 0; }
.scope-title { margin: 0; display: block; font-size: 1.15rem; line-height: 1.4; overflow-wrap: anywhere; }
.compact .scope-title { font-size: inherit; }
.scope-context { margin: 4px 0 0; color: var(--color-muted-text); }
.scope-changes { margin-top: 10px; }
summary { cursor: pointer; color: var(--color-action-blue); }
.scope-stack { margin-top: 16px; }
.scope-stack h3 { margin: 0 0 8px; font-size: 1rem; overflow-wrap: anywhere; }
.scope-change { display: grid; gap: 4px; padding: 10px 0; border-top: 1px solid var(--color-border); overflow-wrap: anywhere; }
.scope-change dl { margin: 4px 0 0; font-size: var(--text-metadata-size); }
.scope-change dl > div { display: flex; gap: 10px; }
.scope-change dt { flex: 0 0 3em; color: var(--color-muted-text); }
.scope-change dd { min-width: 0; margin: 0; }
</style>

<script setup lang="ts">
import { ExternalLink } from "@lucide/vue";
</script>

<template>
  <section id="settings-docs" class="section-panel">
    <div class="section-heading">
      <div>
        <p class="eyebrow">Docs</p>
        <h2>Reference</h2>
      </div>
    </div>
    <div class="settings-doc-links">
      <a
        href="https://github.com/magrhino/wudup/blob/main/docs/CONFIGURATION.md"
        target="_blank"
        rel="noopener noreferrer"
        class="text-link"
      >
        Deployment environment variables
        <ExternalLink :size="15" />
      </a>
      <a
        href="https://github.com/magrhino/wudup/blob/main/docs/examples/settings.env.example"
        target="_blank"
        rel="noopener noreferrer"
        class="text-link"
      >
        WebUI env example
        <ExternalLink :size="15" />
      </a>
    </div>
  </section>
</template>

<style scoped>
#settings-docs {
  scroll-margin-top: 18px;
}

.settings-doc-links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 18px;
  margin-top: 14px;
}

.settings-doc-links .text-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>

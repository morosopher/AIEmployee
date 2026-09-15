<script setup lang="ts">
const props = defineProps<{ source: { source_type: string; source_id: string; provider_url: string | null } }>()
const safeUrl = (() => { if (!props.source.provider_url) return null; try { const url = new URL(props.source.provider_url); return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null } catch { return null } })()
</script>
<template>
  <a
    v-if="safeUrl"
    :href="safeUrl"
    target="_blank"
    rel="noopener noreferrer"
  >{{ source.source_type }} 来源</a>
  <span v-else>{{ source.source_type }} 来源</span>
</template>

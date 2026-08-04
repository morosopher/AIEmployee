<script setup lang="ts">
defineProps<{ source: { source_type: string; source_id: string; provider_url: string | null } }>()
</script>
<template><a v-if="source.provider_url" :href="source.provider_url" target="_blank" rel="noopener noreferrer">{{ source.source_type }} 来源</a><span v-else>{{ source.source_type }} 来源</span></template>

<script setup lang="ts">
defineProps<{ source: { provider: string; object_id: string; label?: string } }>()
</script>
<template><a :href="`/sources/${encodeURIComponent(source.provider)}/${encodeURIComponent(source.object_id)}`" target="_blank" rel="noopener noreferrer">{{ source.label || `${source.provider} 来源` }}</a></template>

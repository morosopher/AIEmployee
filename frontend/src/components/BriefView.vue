<script setup lang="ts">
import MarkdownMessage from './MarkdownMessage.vue'
import SourceLink from './SourceLink.vue'
import type { Brief } from '@/api/types'
defineProps<{ brief: Brief; propose?: (kind: string) => void }>()
</script>
<template>
  <article class="brief-view">
    <header><h1>{{ brief.headline || '今日简报' }}</h1><p>完整性：{{ brief.completeness }} · 数据截止：{{ brief.source_cutoff }}</p></header>
    <div v-if="brief.warnings.length" class="warning" role="alert"><strong>部分数据未完成</strong><ul><li v-for="warning in brief.warnings" :key="warning">{{ warning }}</li></ul></div>
    <MarkdownMessage :content="brief.markdown" />
    <section v-for="item in brief.items" :key="item.position" class="brief-item"><h2>{{ item.title }}</h2><MarkdownMessage :content="item.body_markdown" /><div class="sources"><SourceLink v-for="source in item.source_refs" :key="`${source.provider}:${source.object_id}`" :source="source" /></div><button v-if="item.suggested_action_kind && propose" type="button" @click="propose(item.suggested_action_kind)">创建任务建议</button></section>
  </article>
</template>

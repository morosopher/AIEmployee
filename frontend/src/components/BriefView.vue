<script setup lang="ts">
import MarkdownMessage from './MarkdownMessage.vue'
import SourceLink from './SourceLink.vue'
import type { Brief } from '@/api/types'
import {
  sourceAction,
  type BriefActionKind,
  type BriefSource,
} from '@/features/briefs/sourceActions'

/** 每个按钮紧邻明确来源；建议内容和渲染本身均不能触发创建、审批或发送。 */
const props = defineProps<{
  brief: Brief
  busy?: boolean
  propose?: (kind: BriefActionKind, source: BriefSource) => void | Promise<void>
}>()
function prepare(kind: string | null, source: BriefSource): void {
  const action = sourceAction(kind, source)
  if (action && !props.busy) void props.propose?.(action, source)
}
</script>
<template>
  <article class="brief-view">
    <header>
      <h1>{{ brief.headline || '今日简报' }}</h1>
      <p>
        完整性：{{ brief.completeness }} · 数据截止：{{ brief.source_cutoff }}
      </p>
    </header>
    <div
      v-if="brief.warnings.length"
      class="warning"
      role="alert"
    >
      <strong>部分数据未完成</strong>
      <ul>
        <li
          v-for="warning in brief.warnings"
          :key="warning"
        >
          {{ warning }}
        </li>
      </ul>
    </div>
    <MarkdownMessage :content="brief.markdown" />
    <section
      v-for="item in brief.items"
      :key="item.position"
      class="brief-item"
    >
      <h2>{{ item.title }}</h2>
      <MarkdownMessage :content="item.body_markdown" />
      <div class="sources">
        <div
          v-for="(source, index) in item.source_refs"
          :key="`${source.source_type}:${source.source_id}`"
          class="source-action"
        >
          <SourceLink :source="source" /><button
            v-if="sourceAction(item.suggested_action_kind, source) && propose"
            type="button"
            name="prepare-source-action"
            :disabled="busy"
            @click="prepare(item.suggested_action_kind, source)"
          >
            {{
              item.suggested_action_kind === 'mail.reply'
                ? '创建回复草稿'
                : '创建修改提案'
            }}{{ item.source_refs.length > 1 ? `（来源 ${index + 1}）` : '' }}
          </button>
        </div>
      </div>
    </section>
  </article>
</template>
<style scoped>
.source-action {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin: 0.75rem 0;
}
button:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

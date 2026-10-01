<script setup lang="ts">
import Accordion from 'primevue/accordion'
import AccordionPanel from 'primevue/accordionpanel'
import AccordionHeader from 'primevue/accordionheader'
import AccordionContent from 'primevue/accordioncontent'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
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
/**
 * 点击后再次使用既有来源规则校验；忙碌时不派发回调，渲染本身没有副作用。
 * @param kind 服务端的建议动作；未知动作保持不可执行。
 * @param source 与当前按钮绑定的来源，不按列表顺序推断目标。
 * @returns 无；异步创建由消费页面负责。
 */
function prepare(kind: string | null, source: BriefSource): void {
  const action = sourceAction(kind, source)
  if (action && !props.busy) void props.propose?.(action, source)
}
</script>
<template>
  <article class="space-y-4">
    <Card>
      <template #content>
        <header class="space-y-3">
          <h2 class="text-xl font-semibold">
            {{ brief.headline || '今日简报' }}
          </h2>
          <div class="flex flex-wrap items-center gap-2">
            <!-- 完整性是服务端原文；不把任意字符串映射为任务成功状态。 -->
            <Tag
              severity="secondary"
              :value="`完整性：${brief.completeness}`"
            />
            <p class="break-all text-sm text-muted-color">
              数据截止：{{ brief.source_cutoff }}
            </p>
          </div>
        </header>
        <!-- 保留每条真实警告；现有契约不提供同步时间，不能用数据截止时间冒充。 -->
        <Message
          v-if="brief.warnings.length || brief.completeness === 'partial'"
          severity="warn"
          role="alert"
          class="my-4"
        >
          <strong>部分数据未完成</strong>
          <ul
            v-if="brief.warnings.length"
            class="list-inside list-disc"
          >
            <li
              v-for="warning in brief.warnings"
              :key="warning"
            >
              {{ warning }}
            </li>
          </ul>
          <p>最后同步时间：未提供</p>
        </Message>
        <MarkdownMessage :content="brief.markdown" />
      </template>
    </Card>
    <Card
      v-for="item in brief.items"
      :key="item.position"
    >
      <template #content>
        <section class="space-y-3">
          <h3 class="text-lg font-semibold">
            {{ item.title }}
          </h3>
          <MarkdownMessage :content="item.body_markdown" />
          <!-- 来源初始展开，迁移后无需先发现折叠入口即可访问原来源与创建动作。 -->
          <Accordion
            v-if="item.source_refs.length"
            value="sources"
          >
            <AccordionPanel value="sources">
              <AccordionHeader>{{ item.title }}：来源引用</AccordionHeader>
              <AccordionContent>
                <div class="space-y-3">
                  <div
                    v-for="(source, index) in item.source_refs"
                    :key="`${source.source_type}:${source.source_id}`"
                    class="flex flex-wrap items-center gap-3"
                  >
                    <SourceLink :source="source" />
                    <Button
                      v-if="
                        sourceAction(item.suggested_action_kind, source) &&
                          propose
                      "
                      name="prepare-source-action"
                      severity="secondary"
                      :disabled="busy"
                      @click="prepare(item.suggested_action_kind, source)"
                    >
                      {{
                        item.suggested_action_kind === 'mail.reply'
                          ? '创建回复草稿'
                          : '创建修改提案'
                      }}{{
                        item.source_refs.length > 1
                          ? `（来源 ${index + 1}）`
                          : ''
                      }}
                    </Button>
                  </div>
                </div>
              </AccordionContent>
            </AccordionPanel>
          </Accordion>
        </section>
      </template>
    </Card>
  </article>
</template>

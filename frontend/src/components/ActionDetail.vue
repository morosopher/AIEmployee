<script setup lang="ts">
import { computed, toRef } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Panel from 'primevue/panel'
import Tag from 'primevue/tag'
import Timeline from 'primevue/timeline'
import StatusTag from './StatusTag.vue'
import type { ActionSnapshot, ActionTimelineEvent } from '@/api/types'
import { useActionControls } from '@/features/actions/useActionControls'
import ApprovalCard from './ApprovalCard.vue'
import NeedsAttentionPanel from './NeedsAttentionPanel.vue'
import {
  actionEventLabel,
  actionLabel,
  formatActionTime,
  providerLabel,
  safeProviderUrl,
} from '@/features/actions/presentation'

/** 详情只接收已验证快照；专用组件明确触发审批或核对，所有回执随后重新读取。 */
interface Props {
  snapshot: ActionSnapshot
  timezone: string
}
const props = defineProps<Props>()
const emit = defineEmits<{ changed: [] }>()
const { busy, error, refresh, decide, reconcile, resolve, withdraw } =
  useActionControls(toRef(props, 'snapshot'), () => emit('changed'))
const editorUrl = computed(() =>
  props.snapshot.local_action
    ? `${props.snapshot.local_action.editor_url}?task=${props.snapshot.task_id}`
    : undefined,
)
const canWithdraw = computed(
  () =>
    props.snapshot.approval?.status === 'pending' &&
    !props.snapshot.execution &&
    ['created', 'queued', 'waiting_approval'].includes(props.snapshot.status),
)
const providerUrl = computed(() =>
  safeProviderUrl(props.snapshot.provider_url, props.snapshot.provider),
)
const missingSources = computed(() => {
  const preview = props.snapshot.approval?.preview
  return preview?.kind === 'calendar'
    ? preview.conflicts
        .filter((conflict) => conflict.kind === 'partial_sources')
        .flatMap((conflict) => conflict.missing_connection_ids).length
    : 0
})
/**
 * Live region 迁移：只读核对、正文到期、缺失来源三个 status 与动作错误 alert
 * 逐字保留在 Message；到期 Tag 只是历史标签，不重复 live 播报。Panel/Timeline 无额外公告。
 */
</script>

<template>
  <article class="min-w-0 wrap-anywhere">
    <Panel>
      <template #header="{ id }">
        <h2
          :id="id"
          class="text-lg font-semibold"
        >
          {{ actionLabel(snapshot.action) }}
        </h2>
      </template>
      <div class="space-y-4">
        <p>
          {{ providerLabel(snapshot.provider) }} ·
          <StatusTag
            kind="action"
            :value="snapshot.status"
          />
        </p>
        <p
          v-if="snapshot.status === 'needs_attention'"
          class="rounded-lg border-l-4 border-orange-600 bg-orange-50 p-3 text-color"
        >
          结果需要核实。请先在供应商中查看实际结果。
        </p>
        <Message
          v-if="snapshot.status === 'reconciling'"
          role="status"
          severity="warn"
          aria-live="polite"
        >
          正在只读核对执行结果，请等待服务端更新。
        </Message>
        <p
          v-if="snapshot.error_code"
          class="text-red-700"
        >
          错误代码：{{ snapshot.error_code }}
        </p>
        <Message
          v-if="snapshot.approval?.content_status === 'redacted'"
          role="status"
          severity="warn"
          aria-live="polite"
        >
          内容已到期，仅保留执行历史。
        </Message>
        <Message
          v-if="missingSources"
          class="rounded-lg border-l-4 border-orange-600 bg-orange-50 p-3 text-color"
          role="status"
          severity="warn"
          aria-live="polite"
        >
          部分日历来源尚未同步（{{ missingSources }}
          个连接），冲突检查可能不完整。
        </Message>
        <Tag
          v-if="snapshot.approval?.content_status === 'redacted'"
          value="内容已过期"
          severity="secondary"
        />
        <dl class="grid grid-cols-[minmax(6rem,1fr)_minmax(0,1.5fr)] gap-3">
          <template v-if="snapshot.approval">
            <dt class="text-muted-color">
              审批状态
            </dt>
            <dd class="m-0">
              <StatusTag
                kind="approval"
                :value="snapshot.approval.status"
              />
            </dd>
            <dt class="text-muted-color">
              审批版本 / 冻结版本
            </dt>
            <dd class="m-0">
              {{ snapshot.approval.version }} /
              {{ snapshot.approval.proposal_version }}
            </dd>
          </template>
          <template v-if="snapshot.execution">
            <dt class="text-muted-color">
              写入尝试
            </dt>
            <dd class="m-0">
              {{ snapshot.execution.write_attempt_count }}
            </dd>
            <dt class="text-muted-color">
              核对尝试
            </dt>
            <dd class="m-0">
              {{ snapshot.execution.reconciliation_attempt_count }}
            </dd>
          </template>
          <dt class="text-muted-color">
            最近更新
          </dt>
          <dd class="m-0">
            {{ formatActionTime(snapshot.updated_at, timezone) }}（{{
              timezone
            }}）
          </dd>
        </dl>
        <p v-if="snapshot.execution?.manual_resolution">
          人工结论：{{
            snapshot.execution.manual_resolution === 'confirmed_executed'
              ? '确认已执行'
              : '确认未执行'
          }}。该记录不会自动重新发送或修改日程。
        </p>
        <Button
          v-if="editorUrl"
          :as="RouterLink"
          :to="editorUrl"
          link
          :label="`打开${snapshot.local_action?.item_kind === 'mail_draft' ? '邮件草稿' : '日程提案'}`"
        />
        <ApprovalCard
          v-if="snapshot.approval"
          :approval="snapshot.approval"
          :decide="decide"
          :reload="() => refresh()"
          :editor-url="editorUrl"
          :timezone="timezone"
          :locked="busy"
        />
        <Button
          v-if="canWithdraw"
          name="withdraw-action"
          label="撤回审批以继续编辑"
          :disabled="busy"
          severity="secondary"
          outlined
          @click="withdraw"
        />
        <Message
          v-if="error"
          role="alert"
          severity="error"
        >
          {{ error.message
          }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
        </Message>
        <NeedsAttentionPanel
          v-if="snapshot.status === 'needs_attention'"
          :snapshot="snapshot"
          :reconcile="reconcile"
          :resolve="resolve"
          :reload="() => refresh()"
          :locked="busy"
        />
        <Button
          v-if="providerUrl"
          as="a"
          :href="providerUrl"
          target="_blank"
          rel="noopener noreferrer"
          link
          :label="`在 ${providerLabel(snapshot.provider)} 中检查结果`"
        />
        <h3>执行时间线</h3>
        <!-- 不渲染原始 payload 或完整预览，未知事件只呈现固定兼容文案。 -->
        <Timeline
          v-if="snapshot.timeline.length"
          :value="snapshot.timeline"
          data-key="id"
          aria-label="审计时间线"
          :pt="{
            eventOpposite: { class: 'hidden' },
            eventContent: { class: 'min-w-0 pb-4' },
          }"
        >
          <template #content="{ item: event }: { item: ActionTimelineEvent }">
            <strong>{{ actionEventLabel(event.event) }}</strong>
            <time
              :datetime="event.occurred_at"
              class="mt-1 block text-sm text-muted-color"
            >{{ formatActionTime(event.occurred_at, timezone) }}</time>
          </template>
        </Timeline>
        <p v-else>
          尚无可展示的持久事件。
        </p>
      </div>
    </Panel>
  </article>
</template>

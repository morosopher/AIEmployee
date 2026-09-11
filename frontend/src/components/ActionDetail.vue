<script setup lang="ts">
import { computed } from 'vue'
import type { ActionSnapshot } from '@/api/types'
import {
  actionEventLabel,
  actionLabel,
  actionStatusLabel,
  formatActionTime,
  providerLabel,
  safeProviderUrl,
} from '@/features/actions/presentation'

/** 只读详情只接收已验证快照；审批和编辑交互由专用组件负责。 */
interface Props {
  snapshot: ActionSnapshot
  timezone: string
}
const props = defineProps<Props>()
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
const approvalLabels = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
  expired: '已过期',
  invalidated: '已失效',
}
</script>

<template>
  <article class="action-detail">
    <h2>{{ actionLabel(snapshot.action) }}</h2>
    <p>
      {{ providerLabel(snapshot.provider) }} ·
      {{ actionStatusLabel(snapshot.status) }}
    </p>
    <p
      v-if="snapshot.status === 'needs_attention'"
      class="attention"
    >
      结果需要核实。请先在供应商中查看实际结果。
    </p>
    <p
      v-if="snapshot.status === 'reconciling'"
      role="status"
    >
      正在只读核对执行结果，请等待服务端更新。
    </p>
    <p
      v-if="snapshot.error_code"
      class="error"
    >
      错误代码：{{ snapshot.error_code }}
    </p>
    <p
      v-if="snapshot.approval?.content_status === 'redacted'"
      class="content-expired"
      role="status"
    >
      内容已到期，仅保留执行历史。
    </p>
    <p
      v-if="missingSources"
      class="attention"
      role="status"
    >
      部分日历来源尚未同步（{{ missingSources }} 个连接），冲突检查可能不完整。
    </p>
    <dl>
      <template v-if="snapshot.approval">
        <dt>审批状态</dt>
        <dd>{{ approvalLabels[snapshot.approval.status] }}</dd>
        <dt>审批版本 / 冻结版本</dt>
        <dd>
          {{ snapshot.approval.version }} /
          {{ snapshot.approval.proposal_version }}
        </dd>
      </template>
      <template v-if="snapshot.execution">
        <dt>写入尝试</dt>
        <dd>{{ snapshot.execution.write_attempt_count }}</dd>
        <dt>核对尝试</dt>
        <dd>{{ snapshot.execution.reconciliation_attempt_count }}</dd>
      </template>
      <dt>最近更新</dt>
      <dd>
        {{ formatActionTime(snapshot.updated_at, timezone) }}（{{ timezone }}）
      </dd>
    </dl>
    <p v-if="snapshot.execution?.manual_resolution">
      人工结论：{{
        snapshot.execution.manual_resolution === 'confirmed_executed'
          ? '确认已执行'
          : '确认未执行'
      }}。该记录不会自动重新发送或修改日程。
    </p>
    <a
      v-if="providerUrl"
      :href="providerUrl"
      target="_blank"
      rel="noopener noreferrer"
    >在 {{ providerLabel(snapshot.provider) }} 中检查结果</a>
    <h3>执行时间线</h3>
    <!-- 不渲染原始 payload 或完整预览，未知事件只呈现固定兼容文案。 -->
    <ol
      v-if="snapshot.timeline.length"
      aria-label="审计时间线"
    >
      <li
        v-for="event in snapshot.timeline"
        :key="event.id"
      >
        <strong>{{ actionEventLabel(event.event) }}</strong>
        <time :datetime="event.occurred_at">{{
          formatActionTime(event.occurred_at, timezone)
        }}</time>
      </li>
    </ol>
    <p v-else>
      尚无可展示的持久事件。
    </p>
  </article>
</template>

<style scoped>
.action-detail {
  overflow-wrap: anywhere;
}
h2 {
  margin-top: 0.5rem;
}
dl {
  display: grid;
  grid-template-columns: minmax(6rem, 1fr) minmax(0, 1.5fr);
  gap: 0.75rem;
}
dt {
  color: #485365;
}
dd {
  margin: 0;
}
ol {
  padding-left: 1.25rem;
}
li {
  margin-bottom: 1rem;
}
time {
  display: block;
  color: #485365;
  font-size: 0.875rem;
  margin-top: 0.25rem;
}
.error {
  color: #a61b1b;
}
.attention,
.content-expired {
  padding: 0.75rem;
  background: #fff5df;
  border-left: 3px solid #946200;
}
a {
  color: #164e9c;
}
a:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

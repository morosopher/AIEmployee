<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ProblemError } from '@/api/client'
import type { ActionApproval } from '@/api/types'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'
import { formatActionTime } from '@/features/actions/presentation'
import MailApprovalPreview from './MailApprovalPreview.vue'
import CalendarApprovalPreview from './CalendarApprovalPreview.vue'

/** M1 假工具历史的兼容形状；真实 M2 必须提供严格判别的冻结 preview。 */
interface LegacyApproval {
  id: string
  tool: string
  payload: Record<string, unknown>
  version: number
  payload_hash: string
  status: string
  expires_at: string
}
/** 决定发送原始 version/hash；reload 只能重取快照，不能重放决定。 */
interface Props {
  approval: ActionApproval | LegacyApproval
  decide: (
    id: string,
    decision: 'approved' | 'rejected',
    version: number,
    payloadHash: string,
  ) => Promise<void>
  reload?: () => void | Promise<void>
  editorUrl?: string
  timezone?: string
  locked?: boolean
}
const props = withDefaults(defineProps<Props>(), {
  timezone: 'UTC',
  reload: undefined,
  editorUrl: undefined,
  locked: false,
})
const busy = ref(false),
  decided = ref(false),
  conflict = ref(false)
const error = ref<ActionRecovery | null>(null),
  now = ref(Date.now())
const structured = computed(() =>
  'preview' in props.approval ? props.approval : null,
)
const legacy = computed(() =>
  'tool' in props.approval ? props.approval : null,
)
const expired = computed(
  () =>
    !Number.isFinite(Date.parse(props.approval.expires_at)) ||
    Date.parse(props.approval.expires_at) <= now.value,
)
const disabled = computed(
  () =>
    props.locked ||
    busy.value ||
    decided.value ||
    conflict.value ||
    expired.value ||
    props.approval.status !== 'pending' ||
    structured.value?.content_status === 'redacted',
)
const statusLabels: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
  expired: '已过期',
  invalidated: '已失效',
}
let generation = 0
let expiryTimer: ReturnType<typeof setTimeout> | undefined

/** 到期只禁用本地交互，最终有效性由服务端时钟验证；长截止时间避免计时器溢出。 */
function scheduleExpiry(): void {
  clearTimeout(expiryTimer)
  now.value = Date.now()
  const remaining = Date.parse(props.approval.expires_at) - now.value
  if (remaining > 0)
    expiryTimer = setTimeout(scheduleExpiry, Math.min(remaining, 2_147_483_647))
}
watch(
  () => [
    props.approval.id,
    props.approval.version,
    props.approval.payload_hash,
  ],
  () => {
    generation += 1
    busy.value = false
    decided.value = false
    conflict.value = false
    error.value = null
  },
  { immediate: true },
)
watch(() => props.approval.expires_at, scheduleExpiry, { immediate: true })
onUnmounted(() => {
  generation += 1
  clearTimeout(expiryTimer)
})

/**
 * @param decision 用户明确选择的一次决定。
 * @returns 等待调用方完成权威状态刷新，不推断供应商是否已执行。
 */
async function submitDecision(
  decision: 'approved' | 'rejected',
): Promise<void> {
  now.value = Date.now()
  if (disabled.value) return
  const owner = generation
  busy.value = true
  error.value = null
  try {
    await props.decide(
      props.approval.id,
      decision,
      props.approval.version,
      props.approval.payload_hash,
    )
    if (owner === generation) decided.value = true
  } catch (cause) {
    if (owner !== generation) return
    error.value = actionRecovery(cause)
    conflict.value =
      cause instanceof ProblemError && cause.problem.status === 409
  } finally {
    if (owner === generation) busy.value = false
  }
}
</script>
<template>
  <section
    class="approval-card"
    aria-label="人工审批"
  >
    <h2>人工审批{{ legacy ? `：${legacy.tool}` : '' }}</h2>
    <template v-if="structured?.content_status === 'available'">
      <MailApprovalPreview
        v-if="structured.preview.kind === 'mail'"
        :preview="structured.preview"
      />
      <CalendarApprovalPreview
        v-else
        :preview="structured.preview"
      />
    </template>
    <p
      v-else-if="structured"
      role="status"
    >
      内容已到期，仅保留执行历史。
    </p>
    <dl v-else-if="legacy">
      <template
        v-for="(value, key) in legacy.payload"
        :key="key"
      >
        <dt>{{ key }}</dt>
        <dd>
          {{ typeof value === 'object' ? JSON.stringify(value) : value }}
        </dd>
      </template>
    </dl>
    <p>
      审批版本 {{ approval.version
      }}<template v-if="structured">
        · 冻结版本 {{ structured.proposal_version }} ·
        {{ structured.risk_level === 'high' ? '高风险' : '中风险' }}
      </template>
      ·
      {{
        expired ? '已过期' : statusLabels[approval.status] || approval.status
      }}
    </p>
    <p>
      到期时间：{{ formatActionTime(approval.expires_at, timezone) }}（{{
        timezone
      }}）
    </p>
    <div class="controls">
      <button
        type="button"
        name="approved"
        :disabled="disabled"
        @click="submitDecision('approved')"
      >
        批准
      </button><button
        type="button"
        name="rejected"
        :disabled="disabled"
        @click="submitDecision('rejected')"
      >
        拒绝
      </button>
    </div>
    <p
      v-if="busy || decided"
      role="status"
    >
      {{
        busy ? '正在记录决定…' : '决定已记录，执行结果以服务端后续状态为准。'
      }}
    </p>
    <div
      v-if="error"
      role="alert"
      data-testid="approval-error"
      class="error"
    >
      <p>
        {{ error.message
        }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
      </p>
      <RouterLink
        v-if="error.action === 'reauthorize'"
        to="/connections"
      >
        重新授权
      </RouterLink>
      <RouterLink
        v-else-if="error.action === 'new_version' && editorUrl"
        :to="editorUrl"
      >
        创建新版本
      </RouterLink>
      <button
        v-else-if="reload"
        type="button"
        @click="reload"
      >
        重新加载
      </button>
    </div>
  </section>
</template>
<style scoped>
.approval-card {
  margin-top: 1rem;
  padding: 1rem;
  border: 1px solid #dce2ea;
  border-radius: 0.65rem;
  overflow-wrap: anywhere;
}
h2 {
  font-size: 1.2rem;
}
.controls {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.error {
  color: #a61b1b;
}
button:focus-visible,
a:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

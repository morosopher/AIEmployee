<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
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
/** 仅日程编辑链接携带固定恢复枚举；原任务参数保留，落地后仍须显式重新准备。 */
const recoveryUrl = computed(() => {
  if (!props.editorUrl) return undefined
  const url = new URL(props.editorUrl, 'https://local.invalid')
  if (
    url.origin !== 'https://local.invalid' ||
    !/^\/calendar\/proposals\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(
      url.pathname,
    )
  )
    return props.editorUrl
  url.searchParams.set('recovery', 'new_version')
  return `${url.pathname}${url.search}${url.hash}`
})
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
let countdownTimer: ReturnType<typeof setTimeout> | undefined
const displayNow = ref(Date.now())
/** 仅作显示，不写入原 now/expired 或审批状态；秒级公告为 polite，避免 assertive 打断。 */
const remainingSeconds = computed(() =>
  Math.max(
    0,
    Math.ceil(
      (Date.parse(props.approval.expires_at) - displayNow.value) / 1000,
    ),
  ),
)
const legacyRows = computed(() =>
  Object.entries(legacy.value?.payload ?? {}).map(([field, value]) => ({
    field,
    value: typeof value === 'object' ? JSON.stringify(value) : value,
  })),
)
/** 展示 timer 只在待审批且有效的截止时间运行；原最长截止 timer 与服务端裁决不变。 */
function scheduleCountdown(): void {
  clearTimeout(countdownTimer)
  displayNow.value = Date.now()
  const remaining = Date.parse(props.approval.expires_at) - displayNow.value
  if (props.approval.status === 'pending' && remaining > 0)
    countdownTimer = setTimeout(scheduleCountdown, Math.min(remaining, 1000))
}

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
watch(
  () => [props.approval.expires_at, props.approval.status],
  scheduleCountdown,
  { immediate: true },
)
onUnmounted(() => {
  generation += 1
  clearTimeout(expiryTimer)
  clearTimeout(countdownTimer)
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
  <Card
    role="region"
    aria-label="人工审批"
    class="mt-4 min-w-0 wrap-anywhere"
  >
    <template #title>
      <h2 class="text-lg font-semibold">
        人工审批{{ legacy ? `：${legacy.tool}` : '' }}
      </h2>
    </template>
    <template #content>
      <div class="space-y-4">
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
        <Message
          v-else-if="structured"
          severity="info"
          role="status"
          aria-live="polite"
        >
          内容已到期，仅保留执行历史。
        </Message>
        <div
          v-else-if="legacy"
          class="overflow-x-auto"
          tabindex="0"
          aria-label="历史工具载荷滚动区域"
        >
          <DataTable
            :value="legacyRows"
            data-key="field"
            :table-props="{ 'aria-label': '历史工具冻结载荷' }"
          >
            <Column
              field="field"
              header="字段"
              :pt="{
                headerCell: { scope: 'col' },
                bodyCell: { role: 'rowheader' },
              }"
            />
            <Column
              field="value"
              header="精确载荷"
              class="whitespace-pre-wrap wrap-anywhere"
              :pt="{ headerCell: { scope: 'col' } }"
            />
          </DataTable>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <p>
            审批版本 {{ approval.version
            }}<template v-if="structured">
              · 冻结版本 {{ structured.proposal_version }}
            </template>
          </p>
          <Tag
            v-if="structured"
            :value="structured.risk_level === 'high' ? '高风险' : '中风险'"
            :severity="structured.risk_level === 'high' ? 'danger' : 'warn'"
          />
          <span>{{
            expired
              ? '已过期'
              : statusLabels[approval.status] || approval.status
          }}</span>
        </div>
        <p>
          到期时间：{{ formatActionTime(approval.expires_at, timezone) }}（{{
            timezone
          }}）
        </p>
        <!-- 到期提示与倒计时共用一个真实 status；不替代原 expired/disabled 判断。 -->
        <Message
          v-if="approval.status === 'pending'"
          severity="info"
          role="status"
          aria-live="polite"
        >
          <template v-if="expired">
            审批已过期，请重新加载权威状态。
          </template>
          <template v-else>
            审批剩余 {{ Math.floor(remainingSeconds / 60) }} 分
            {{ remainingSeconds % 60 }} 秒
          </template>
        </Message>
        <div class="flex flex-wrap gap-3">
          <Button
            type="button"
            name="approved"
            label="批准"
            :disabled="disabled"
            :loading="busy"
            :aria-busy="busy"
            @click="submitDecision('approved')"
          />
          <Button
            type="button"
            name="rejected"
            label="拒绝"
            severity="secondary"
            :disabled="disabled"
            :loading="busy"
            :aria-busy="busy"
            @click="submitDecision('rejected')"
          />
        </div>
        <!-- 原记录中／已记录共用 status 原文保留；Message 不嵌套新的 live region。 -->
        <Message
          v-if="busy || decided"
          severity="info"
          role="status"
          aria-live="polite"
        >
          {{
            busy
              ? '正在记录决定…'
              : '决定已记录，执行结果以服务端后续状态为准。'
          }}
        </Message>
        <Message
          v-if="error"
          severity="error"
          role="alert"
        >
          <p>
            {{ error.message
            }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
          </p>
          <Button
            v-if="error.action === 'reauthorize'"
            :as="RouterLink"
            to="/connections"
            label="重新授权"
            link
          />
          <Button
            v-else-if="error.action === 'new_version' && editorUrl"
            :as="RouterLink"
            :to="recoveryUrl ?? editorUrl"
            label="创建新版本"
            link
          />
          <Button
            v-else-if="reload"
            type="button"
            label="重新加载"
            @click="reload"
          />
        </Message>
      </div>
    </template>
  </Card>
</template>

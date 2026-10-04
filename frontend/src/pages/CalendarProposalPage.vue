<script setup lang="ts">
import {
  computed,
  onMounted,
  onUnmounted,
  defineAsyncComponent,
  defineComponent,
  h,
  ref,
  watch,
} from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { useConfirm } from 'primevue/useconfirm'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import type { CalendarConfirmation } from '@/api/types'
import { useConnectionCatalog } from '@/composables/useConnectionCatalog'
import { useCalendarProposalEditor } from '@/features/calendar/useCalendarProposalEditor'
import { useCalendarRestore } from '@/features/calendar/useCalendarRestore'
import { useCalendarReprepare } from '@/features/calendar/useCalendarReprepare'
import { useLocalActionCreation } from '@/features/actions/useLocalActionCreation'
import {
  actionStatusLabel,
  formatActionTime,
} from '@/features/actions/presentation'
import LocalEditorFrame from '@/components/LocalEditorFrame.vue'
import CalendarFieldsComparison from '@/components/CalendarFieldsComparison.vue'
import CalendarConflictNotice from '@/components/CalendarConflictNotice.vue'
import MissingCalendarConnections from '@/components/MissingCalendarConnections.vue'
import CalendarRepreparePanel from '@/components/CalendarRepreparePanel.vue'
import EditorRecovery from '@/components/EditorRecovery.vue'
import LinkedActionPanel from '@/components/LinkedActionPanel.vue'

/** 重型表单只在打开编辑器后加载；失败须显式重试，不能把加载失败伪装成空白提案。 */
const moduleFailed = ref(false)
const CalendarEditorForm = defineAsyncComponent({
  loader: () => import('@/components/CalendarEditorForm.vue'),
  delay: 0,
  loadingComponent: defineComponent({
    setup: () => () =>
      moduleFailed.value
        ? null
        : h('div', { class: 'space-y-2' }, [
            h(
              Message,
              { role: 'status', severity: 'secondary', 'aria-live': 'polite' },
              () => '正在加载日程表单…',
            ),
            h(Skeleton, { height: '12rem' }),
          ]),
  }),
  onError() {
    moduleFailed.value = true
  },
})
/** 显式整页重试重新取得权威提案；不在本地复制请求、状态或重放写操作。 */
function retryForm(): void {
  window.location.reload()
}

/** 页面只组合服务端前后值、确认与冲突事实；不实现排程算法或本地审批状态机。 */
const route = useRoute(),
  router = useRouter()
const proposalId = computed(() =>
  typeof route.params.proposalId === 'string' ? route.params.proposalId : '',
)
const {
  entries,
  loading: catalogLoading,
  error: catalogError,
  load: loadCatalog,
} = useConnectionCatalog()
const {
  proposal,
  form,
  loading,
  busy,
  locked: proposalLocked,
  error,
  dirty,
  fieldsDirty,
  sourceDirty,
  conflicts,
  candidates,
  after,
  canSubmit,
  reload,
  save,
  confirm,
  suggest,
  choose,
  submit,
} = useCalendarProposalEditor(proposalId, (taskId) =>
  router.push({ path: '/actions', query: { task: taskId } }),
)
const {
  source: restoreSource,
  busy: restoreBusy,
  error: restoreError,
  prepare: prepareRestore,
} = useCalendarRestore(proposal, busy, (taskId) =>
  router.push({ path: '/tasks', query: { task_id: taskId } }),
)
const {
  eligible: reprepareEligible,
  visible: reprepareVisible,
  source: reprepareSource,
  busy: reprepareBusy,
  error: reprepareError,
  canPrepare,
  syncTaskId,
  syncStatus,
  syncRunning,
  prepare: prepareVersion,
  sync: syncReprepare,
  refresh: refreshReprepare,
} = useCalendarReprepare(
  proposalId,
  proposal,
  computed(() => busy.value || restoreBusy.value),
  computed(
    () =>
      route.query.recovery === 'new_version' ||
      error.value?.action === 'new_version',
  ),
  reload,
  (id) => router.push(`/calendar/proposals/${id}`),
)
// 恢复期间旧输入持续锁定；新路由没有恢复枚举，读取新 editing 后才能独立编辑。
const locked = computed(
  () =>
    proposalLocked.value ||
    reprepareVisible.value ||
    reprepareBusy.value ||
    restoreBusy.value,
)
const {
  newCalendar,
  error: creationError,
  busy: creating,
} = useLocalActionCreation()
/** 仅展示当前显式审批提交的加载态；实际互斥与版本仍由原 hook 负责。 */
const submitting = ref(false)
watch(busy, (value) => { if (!value) submitting.value = false })
const confirmations: Array<{ kind: CalendarConfirmation; label: string }> = [
  { kind: 'time', label: '时间' },
  { kind: 'attendees', label: '参会人' },
  { kind: 'notification_policy', label: '通知策略' },
]
const confirmation = useConfirm()
/** 仅为等待用户决定提供页面级互斥；真正重读、意图复用和创建仍由原 hook 执行。 */
const confirmingPreparation = ref(false)
let preparationGeneration = 0

/**
 * 只结算当前确认一次；旧 accept／reject／hide 即使晚到也不能影响新确认。
 * @param generation 打开确认时捕获的 UI 代次，不是业务版本或请求意图。
 * @param accepted 只有用户确认按钮为 true，取消、Esc 和关闭都为 false。
 */
function finishPreparation(generation: number, accepted: boolean): void {
  if (!confirmingPreparation.value || generation !== preparationGeneration) return
  confirmingPreparation.value = false
  preparationGeneration += 1
  if (accepted && canPrepare.value) void prepareVersion()
}
/**
 * 所有三处新版本入口共用本确认；文案不包含日程正文或敏感摘要。
 * 等待时保留触发按钮供焦点归还，AppShell inert 隔离背景；代次门禁同时拒绝重复 DOM 事件。
 */
function confirmPreparation(): void {
  if (confirmingPreparation.value || !canPrepare.value) return
  confirmingPreparation.value = true
  const generation = ++preparationGeneration
  confirmation.require({
    header: '准备新版本',
    message: '将重新读取来源并创建本地新提案。原提案和审批保留不变。新提案需要独立核对、逐项确认并提交审批。',
    defaultFocus: 'reject',
    rejectProps: { label: '取消', severity: 'secondary', outlined: true },
    acceptProps: { label: '确认准备' },
    accept: () => finishPreparation(generation, true),
    reject: () => finishPreparation(generation, false),
    onHide: () => finishPreparation(generation, false),
  })
}
/** 路由或来源投影变化时关闭旧 UI 决定，不把新对象沿用为旧确认的目标。 */
function cancelPreparation(): void {
  if (!confirmingPreparation.value) return
  finishPreparation(preparationGeneration, false)
  confirmation.close()
}
watch(
  [
    () => route.fullPath,
    proposal,
    () => proposal.value?.version,
    () => proposal.value?.status,
    () => proposal.value?.connection_id,
    () => proposal.value?.calendar_id,
    () => proposal.value?.target_event_id,
    () => proposal.value?.base_etag,
    () => proposal.value?.before_snapshot_id,
    () => reprepareSource.value?.event_id,
    () => reprepareSource.value?.requires_sync,
    canPrepare,
    reprepareVisible,
  ],
  cancelPreparation,
  { flush: 'sync' },
)
let settleLeave: ((accepted: boolean) => void) | null = null
/** 关闭或后续导航只结算当前确认；不把关闭视为批准。 */
function finishLeave(accepted: boolean): void {
  const settle = settleLeave
  settleLeave = null
  settle?.(accepted)
}
/** 页面只提示丢弃本地输入；认证跳登录继续沿用原会话和卸载清理边界。 */
const removeLeaveGuard = router.beforeEach((to, from) => {
  cancelPreparation()
  if (!dirty.value || to.path === from.path || to.path === '/login') return true
  finishLeave(false)
  return new Promise<boolean>((resolve) => {
    settleLeave = resolve
    confirmation.require({
      header: '离开日程编辑器',
      message: '提案尚未保存或确认，确定离开？',
      defaultFocus: 'reject',
      rejectProps: { label: '继续编辑', severity: 'secondary', outlined: true },
      acceptProps: { label: '放弃修改并离开' },
      accept: () => finishLeave(true),
      reject: () => finishLeave(false),
      onHide: () => finishLeave(false),
    })
  })
})
/** 浏览器卸载只能使用原生固定提示，不提供正文摘录，也不写浏览器存储。 */
function beforeUnload(event: BeforeUnloadEvent): void {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
onMounted(() => {
  void loadCatalog()
  window.addEventListener('beforeunload', beforeUnload)
})
onUnmounted(() => {
  cancelPreparation()
  removeLeaveGuard()
  window.removeEventListener('beforeunload', beforeUnload)
  if (settleLeave) {
    finishLeave(false)
    confirmation.close()
  }
})
</script>
<template>
  <LocalEditorFrame title="日程提案">
    <RouterLink
      to="/actions"
      class="text-primary underline"
    >
      返回操作中心
    </RouterLink>
    <p class="rounded-lg border-l-4 border-orange-600 bg-orange-50 p-3">
      保存和确认只修改本地提案。提交审批后，请核对冻结的时间、目标日历和通知策略。
    </p>
    <Message
      v-if="loading || catalogLoading"
      role="status"
      aria-live="polite"
      severity="secondary"
    >
      正在加载提案与日历目录…
    </Message>
    <EditorRecovery
      v-if="!proposal || moduleFailed"
      :error="
        error || reprepareError || restoreError || catalogError || creationError
      "
      :busy="busy || reprepareBusy || restoreBusy || creating"
      :new-version-available="
        reprepareEligible && !!reprepareSource && !reprepareSource.requires_sync
      "
      @reload="reload"
      @new-object="newCalendar"
      @new-version="confirmPreparation"
    />
    <Button
      type="button"
      name="reload-editor"
      :disabled="busy || restoreBusy || reprepareBusy || confirmingPreparation"
      @click="reload"
    >
      重新加载提案
    </Button>
    <template v-if="proposal">
      <p>
        版本 {{ proposal.version }} · {{ actionStatusLabel(proposal.status) }}
      </p>
      <CalendarRepreparePanel
        v-if="reprepareVisible"
        :connection-id="proposal.connection_id"
        :entries="entries"
        :source="reprepareSource"
        :busy="busy || reprepareBusy || restoreBusy || confirmingPreparation"
        :can-prepare="canPrepare"
        :sync-task-id="syncTaskId"
        :sync-status="syncStatus"
        :sync-running="syncRunning"
        @prepare="confirmPreparation"
        @sync="syncReprepare"
        @refresh="refreshReprepare"
      />
      <section
        v-if="
          proposal.operation_kind === 'update' && proposal.status === 'applied'
        "
        aria-label="恢复已应用修改"
      >
        <p>恢复会准备新的提案，读取当前日程后仍需核对通知策略并重新审批。</p>
        <Button
          v-if="restoreSource"
          type="button"
          name="prepare-restore"
          :disabled="busy || restoreBusy"
          @click="prepareRestore"
        >
          {{ restoreBusy ? '正在创建准备任务…' : '准备恢复提案' }}
        </Button>
        <Message
          v-else
          role="status"
          aria-live="polite"
          severity="secondary"
        >
          恢复来源已不可用，请核对历史快照保留期和目标日程。
        </Message>
      </section>
      <CalendarEditorForm
        :model-value="form"
        :proposal="proposal"
        :entries="entries"
        :locked="locked"
        :busy="busy"
        :dirty="dirty"
        :fields-dirty="fieldsDirty"
        :source-dirty="sourceDirty"
        :error="
          error ||
            reprepareError ||
            restoreError ||
            catalogError ||
            creationError
        "
        :recovery-busy="busy || reprepareBusy || restoreBusy || creating"
        :new-version-available="
          reprepareEligible &&
            !!reprepareSource &&
            !reprepareSource.requires_sync
        "
        @save="save"
        @confirm-calendar="confirm('calendar')"
        @reload="reload"
        @new-object="newCalendar"
        @new-version="confirmPreparation"
      />
      <Message
        v-if="moduleFailed"
        severity="error"
      >
        日程表单加载失败，请重试。重试会重新加载页面。
        <Button
          type="button"
          label="重试加载表单"
          severity="secondary"
          @click="retryForm"
        />
      </Message>
      <section aria-label="逐项确认">
        <h2>确认当前已保存版本</h2>
        <p>待确认：{{ proposal.required_confirmations.length }} 项</p>
        <div class="flex flex-wrap gap-3">
          <Button
            v-for="item in confirmations"
            :key="item.kind"
            type="button"
            :name="`confirm-${item.kind}`"
            :disabled="
              locked ||
                dirty ||
                !proposal.required_confirmations.includes(item.kind)
            "
            @click="confirm(item.kind)"
          >
            确认{{ item.label
            }}{{
              proposal.required_confirmations.includes(item.kind)
                ? ''
                : '（已确认）'
            }}
          </Button>
        </div>
      </section>
      <Message
        v-if="proposal.editor_facts?.before_status === 'unavailable'"
        severity="error"
        role="alert"
      >
        原始修改前快照已不可用，无法核对前后差异。请返回来源重新创建提案。
      </Message>
      <!-- 修改或恢复缺少原快照时只展示输入与错误，不能把 null before 误标为新建。 -->
      <CalendarFieldsComparison
        v-if="
          after &&
            (proposal.operation_kind === 'create' ||
              proposal.editor_facts?.before_status === 'available')
        "
        :before="proposal.editor_facts?.before ?? null"
        :after="after"
      />
      <CalendarConflictNotice
        v-if="conflicts !== null"
        :conflicts="conflicts"
        :timezone="form.timezone"
        :entries="entries"
      />
      <Message
        v-else
        role="status"
        aria-live="polite"
        severity="secondary"
      >
        尚无当前输入的冲突检查，请补齐并保存后重新检查。未检查参会人可用性。
      </Message>
      <Button
        type="button"
        name="suggest-times"
        :disabled="locked || dirty || !after || form.all_day"
        @click="suggest"
      >
        查询三个候选时间
      </Button>
      <section
        v-if="candidates"
        aria-label="服务端候选时间"
      >
        <Message
          v-if="candidates.completeness === 'partial'"
          role="status"
          aria-live="polite"
          severity="warn"
        >
          部分日历来源缺失（{{ candidates.missing_connections.length }}
          个连接）。
        </Message>
        <MissingCalendarConnections
          v-if="candidates.completeness === 'partial'"
          :connection-ids="candidates.missing_connections"
          :entries="entries"
        />
        <p>未检查参会人可用性。选择候选后仍需保存并确认。</p>
        <p v-if="!candidates.candidates.length">
          当前范围没有可用候选，可调整时间后重试。
        </p>
        <div class="flex flex-wrap gap-3">
          <Button
            v-for="candidate in candidates.candidates"
            :key="candidate.starts_at"
            type="button"
            name="choose-candidate"
            :disabled="locked"
            @click="choose(candidate)"
          >
            {{ formatActionTime(candidate.starts_at, form.timezone) }} 至
            {{ formatActionTime(candidate.ends_at, form.timezone) }}（{{
              form.timezone
            }}）
          </Button>
        </div>
      </section>
      <p
        v-if="form.notification_policy === 'none'"
        class="rounded-lg border-l-4 border-orange-600 bg-orange-50 p-3"
      >
        不发送通知仍可能触发供应商的外部同步；请在审批预览中核对供应商警告。
      </p>
      <p
        v-if="
          proposal.operation_kind !== 'create' &&
            !proposal.changed_fields.length
        "
      >
        尚无真实字段变更，不能提交空修改。
      </p>
      <Button
        type="button"
        name="submit-proposal"
        :loading="busy && submitting"
        :aria-busy="busy && submitting"
        :disabled="!canSubmit || locked"
        @click="submitting = true; submit()"
      >
        提交审批
      </Button>
      <LinkedActionPanel
        :local-id="proposal.id"
        item-kind="calendar_proposal"
        :task-query="route.query.task"
        @changed="reload"
      />
    </template>
  </LocalEditorFrame>
</template>

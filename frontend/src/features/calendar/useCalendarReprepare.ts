import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { createCalendarProposal } from '@/api/calendar'
import { syncConnection } from '@/api/connections'
import { createRequestIntent, type RequestIntent } from '@/api/requestIntent'
import type { CalendarProposal } from '@/api/types'
import { useTaskEvents } from '@/composables/useTaskEvents'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'
import { useTasksStore } from '@/stores/tasks'

/**
 * @param proposalId 当前路由身份；切换后原请求回执不能导航或覆盖状态。
 * @param proposal 已验证的服务端编辑投影，只接受明确可重新准备的 update。
 * @param locked 其他编辑请求的互斥状态，避免读取与变更同时发生。
 * @param requested 严格恢复枚举或当前失效错误，只展示入口，不自动创建。
 * @param reload 读取并采纳当前 GET；失败或路由失效返回 null。
 * @param followProposal 只打开新 editing 对象，独立编辑、确认和提交由新页面处理。
 * @returns 来源、同步任务进度及显式恢复动作；不复制旧批准或重放外部命令。
 */
export function useCalendarReprepare(
  proposalId: Ref<string>,
  proposal: Ref<CalendarProposal | null>,
  locked: Ref<boolean>,
  requested: Ref<boolean>,
  reload: () => Promise<CalendarProposal | null>,
  followProposal: (proposalId: string) => Promise<unknown>,
) {
  const busy = ref(false),
    initiated = ref(false),
    error = ref<ActionRecovery | null>(null),
    syncTaskId = ref<string | null>(null)
  const tasks = useTasksStore()
  useTaskEvents(syncTaskId)
  const eligible = computed(() => canReprepare(proposal.value))
  const visible = computed(
    () =>
      eligible.value &&
      (proposal.value?.status !== 'editing' ||
        requested.value ||
        initiated.value),
  )
  const source = computed(() =>
    eligible.value
      ? (proposal.value?.editor_facts?.reprepare_source ?? null)
      : null,
  )
  const syncStatus = computed(() =>
    syncTaskId.value ? (tasks.tasks[syncTaskId.value]?.status ?? null) : null,
  )
  const syncRunning = computed(
    () =>
      syncTaskId.value !== null &&
      !['succeeded', 'failed', 'cancelled'].includes(syncStatus.value ?? ''),
  )
  const canPrepare = computed(
    () =>
      source.value !== null &&
      !source.value.requires_sync &&
      !busy.value &&
      !locked.value &&
      !syncRunning.value,
  )
  let intent: { eventId: string; value: RequestIntent } | null = null,
    epoch = 0,
    disposed = false
  watch(
    proposalId,
    () => {
      epoch += 1
      busy.value = false
      initiated.value = false
      error.value = null
      syncTaskId.value = null
      intent = null
    },
    { flush: 'sync' },
  )
  onUnmounted(() => {
    disposed = true
    epoch += 1
  })

  /** 同一原提案反复 GET 不更换未知创建结果的请求键；只用新 GET 证明的本地事件 UUID。 */
  async function prepare(): Promise<void> {
    if (!canPrepare.value) return
    const owner = epoch
    initiated.value = true
    busy.value = true
    error.value = null
    try {
      const latest = await reload()
      if (disposed || owner !== epoch || !canReprepare(latest)) return
      const verified = latest.editor_facts?.reprepare_source
      if (!verified || verified.requires_sync) return
      if (intent && intent.eventId !== verified.event_id)
        throw new Error('Reprepare source changed during an unresolved intent')
      intent ??= { eventId: verified.event_id, value: createRequestIntent() }
      const created = await createCalendarProposal(
        {
          operation_kind: 'update',
          initialization: 'shell',
          event_id: verified.event_id,
        },
        intent.value,
      )
      if (disposed || owner !== epoch) return
      if (
        created.id === latest.id ||
        created.status !== 'editing' ||
        created.operation_kind !== 'update' ||
        created.connection_id !== latest.connection_id ||
        created.calendar_id !== latest.calendar_id ||
        created.target_event_id !== latest.target_event_id
      )
        throw new Error('Invalid reprepare result binding')
      await followProposal(created.id)
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === epoch) busy.value = false
    }
  }

  /** 复用已有连接同步入口和按连接保留的请求意图；任务回执只证明已入队。 */
  async function sync(): Promise<void> {
    const current = proposal.value
    if (
      !current ||
      !source.value?.requires_sync ||
      busy.value ||
      locked.value ||
      syncRunning.value
    )
      return
    const owner = epoch
    initiated.value = true
    busy.value = true
    error.value = null
    try {
      const receipt = await syncConnection(current.connection_id)
      if (!disposed && owner === epoch)
        syncTaskId.value = receipt.calendar_task_id
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === epoch) busy.value = false
    }
  }

  /** 任务结束后仍由用户显式重读；同步成功不自动创建提案或代替 ETag 资格判断。 */
  async function refresh(): Promise<void> {
    if (busy.value || locked.value || syncRunning.value) return
    const owner = epoch
    busy.value = true
    error.value = null
    try {
      await reload()
    } finally {
      if (!disposed && owner === epoch) busy.value = false
    }
  }
  return {
    eligible,
    visible,
    source,
    busy,
    error,
    canPrepare,
    syncTaskId,
    syncStatus,
    syncRunning,
    prepare,
    sync,
    refresh,
  }
}

/** 状态门禁与服务端投影一致，审批中、执行中、未知结果及已应用对象不提供新版本入口。 */
function canReprepare(
  value: CalendarProposal | null,
): value is CalendarProposal {
  return (
    value?.operation_kind === 'update' &&
    ['editing', 'stale', 'cancelled'].includes(value.status)
  )
}

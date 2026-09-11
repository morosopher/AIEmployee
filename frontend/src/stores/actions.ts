import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getAction,
  listActions,
  resolveAction,
  toolExecutionStatuses,
} from '@/api/actions'
import { ProblemError } from '@/api/client'
import {
  asEventCursor,
  asTaskStatus,
  compareEventCursors,
  type ActionFilters,
  type ActionListItem,
  type ActionSnapshot,
  type ManualResolution,
  type TaskEvent,
} from '@/api/types'

/** 只暴露固定恢复提示和公开 trace，不保存异常详情或供应商原始响应。 */
export interface ActionLoadError {
  message: string
  trace_id: string | null
}

/**
 * 统一操作中心的内存投影。REST 替换完整对象，SSE 仅更新无内容状态/尝试字段。
 * 未提交本地对象始终保持 task_id=null，不建立第二条用户级事件流。
 */
export const useActionsStore = defineStore('actions', () => {
  const items = ref<ActionListItem[]>([])
  const snapshots = ref<Record<string, ActionSnapshot>>({})
  const filters = ref<ActionFilters>({})
  const loading = ref(false)
  const loaded = ref(false)
  const limit = ref(50)
  const offset = ref(0)
  const listError = ref<ActionLoadError | null>(null)
  const snapshotLoading = ref<Record<string, boolean>>({})
  const snapshotErrors = ref<Record<string, ActionLoadError | null>>({})
  const mutating = ref<Record<string, boolean>>({})
  const latestSequences = new Map<string, string>()
  const pendingSnapshots = new Map<string, Promise<void>>()
  const refreshAgain = new Set<string>()
  let epoch = 0
  let listGeneration = 0
  let projectionGeneration = 0

  /**
   * 重取当前服务端筛选页，拒绝过时筛选或新快照/事件抵达之前发出的迟到列表。
   * @param nextFilters 已验证的内容无关筛选项。
   * @returns 请求结束；失败保留上一有效列表并公开恢复提示。
   */
  async function refreshList(
    nextFilters: ActionFilters = filters.value,
  ): Promise<void> {
    filters.value = { ...nextFilters }
    const generation = ++listGeneration,
      owner = epoch,
      observedProjection = projectionGeneration
    loading.value = true
    listError.value = null
    try {
      const page = await listActions(nextFilters)
      if (owner !== epoch || generation !== listGeneration) return
      if (observedProjection !== projectionGeneration) {
        await refreshList()
        return
      }
      items.value = page.items
      limit.value = page.limit
      offset.value = page.offset
      loaded.value = true
    } catch (error) {
      if (owner === epoch && generation === listGeneration)
        listError.value = loadError(error)
    } finally {
      if (owner === epoch && generation === listGeneration)
        loading.value = false
    }
  }

  /**
   * 合并同任务的并发读取；请求中有新事件时最多补取下一轮，旧快照不能回退投影。
   * @param taskId 权威任务 ID。
   * @param minimumCursor mutation 已返回的新游标；不足时保留旧投影并显示恢复提示。
   * @returns 读取结束，不把失败伪装为业务状态。
   */
  function loadSnapshot(taskId: string, minimumCursor?: string): Promise<void> {
    // mutation 的已提交版本同样是权威水位。先合并水位，再复用在途读取，避免丢失新版本要求。
    if (minimumCursor !== undefined) {
      const latest = latestSequences.get(taskId)
      if (
        latest === undefined ||
        compareEventCursors(minimumCursor, latest) > 0
      )
        latestSequences.set(taskId, minimumCursor)
    }
    const existing = pendingSnapshots.get(taskId)
    if (existing) {
      // 该请求可能早于 mutation 提交；至多再读取一轮，不把旧响应当成确认后的恢复结果。
      if (minimumCursor !== undefined) refreshAgain.add(taskId)
      return existing
    }
    const owner = epoch
    const pending = (async () => {
      do {
        refreshAgain.delete(taskId)
        snapshotLoading.value[taskId] = true
        snapshotErrors.value[taskId] = null
        try {
          const snapshot = await getAction(taskId)
          if (owner !== epoch) return
          const latest = latestSequences.get(taskId)
          if (
            (latest !== undefined &&
              compareEventCursors(snapshot.event_cursor, latest) < 0) ||
            (minimumCursor !== undefined &&
              compareEventCursors(snapshot.event_cursor, minimumCursor) < 0)
          ) {
            // 一次旧 REST 结果不能覆盖更晚事件。新事件已标记补取；没有新事件则让用户重试。
            if (!refreshAgain.has(taskId))
              snapshotErrors.value[taskId] = loadError(null)
            continue
          }
          const previousSnapshot = snapshots.value[taskId]
          if (
            !previousSnapshot ||
            compareEventCursors(
              snapshot.event_cursor,
              previousSnapshot.event_cursor,
            ) > 0
          ) {
            // 列表没有任务版本，不能证明在途 GET 已包含这次权威进展；重新按原筛选和分页读取，
            // 不把缓存详情无条件覆盖到后续列表，避免把已移出筛选页的任务重新插入。
            projectionGeneration += 1
          }
          snapshots.value[taskId] = snapshot
          latestSequences.set(taskId, snapshot.event_cursor)
          items.value = items.value.map((item) =>
            item.item_kind === 'trusted_task' && item.task_id === taskId
              ? { ...item, status: snapshot.status }
              : item,
          )
        } catch (error) {
          if (owner === epoch) snapshotErrors.value[taskId] = loadError(error)
        }
      } while (owner === epoch && refreshAgain.has(taskId))
    })().finally(() => {
      if (pendingSnapshots.get(taskId) === pending)
        pendingSnapshots.delete(taskId)
      if (owner === epoch) snapshotLoading.value[taskId] = false
    })
    pendingSnapshots.set(taskId, pending)
    return pending
  }

  /**
   * 确定性投影最新持久事件；不使用 payload 中的正文、预览、人工结论或任意扩展字段。
   * @param event 统一 EventSource 边界已验证的事件。
   * @returns 无返回值；终态、人工处理及未知事件自动重取服务端快照。
   */
  function applyEvent(event: TaskEvent): void {
    if (event.event === 'assistant.delta' || event.event === 'heartbeat') return
    const sequence = asEventCursor(event.sequence)
    const latest = latestSequences.get(event.task_id)
    if (
      sequence === null ||
      (latest !== undefined && compareEventCursors(sequence, latest) <= 0)
    )
      return
    latestSequences.set(event.task_id, sequence)
    projectionGeneration += 1
    const snapshot = snapshots.value[event.task_id]
    const status =
      event.event === 'task.status_changed'
        ? asTaskStatus(event.payload.status)
        : null
    if (status) {
      if (snapshot)
        snapshots.value[event.task_id] = {
          ...snapshot,
          status,
          error_code:
            typeof event.payload.error_code === 'string' ||
            event.payload.error_code === null
              ? event.payload.error_code
              : snapshot.error_code,
        }
      items.value = items.value.map((item) =>
        item.item_kind === 'trusted_task' && item.task_id === event.task_id
          ? { ...item, status }
          : item,
      )
    } else if (snapshot?.execution && event.event.startsWith('tool.')) {
      const execution = { ...snapshot.execution }
      const toolStatus = toolExecutionStatuses.find(
        (value) => value === event.payload.status,
      )
      if (toolStatus) execution.status = toolStatus
      for (const key of [
        'write_attempt_count',
        'reconciliation_attempt_count',
      ] as const) {
        const count = event.payload[key]
        if (
          typeof count === 'number' &&
          Number.isSafeInteger(count) &&
          count >= 0
        )
          execution[key] = count
      }
      if (
        typeof event.payload.error_code === 'string' ||
        event.payload.error_code === null
      )
        execution.error_code = event.payload.error_code
      snapshots.value[event.task_id] = {
        ...snapshot,
        execution,
        reconciliation_attempt_count: execution.reconciliation_attempt_count,
      }
    }
    const statusRequiresSnapshot =
      status === 'succeeded' ||
      status === 'failed' ||
      status === 'cancelled' ||
      status === 'needs_attention'
    const contentFreeStep =
      event.event === 'step.started' ||
      event.event === 'step.completed' ||
      event.event === 'step.failed'
    if (
      !snapshot ||
      statusRequiresSnapshot ||
      (event.event !== 'task.status_changed' && !contentFreeStep)
    ) {
      if (pendingSnapshots.has(event.task_id)) refreshAgain.add(event.task_id)
      void loadSnapshot(event.task_id)
    }
  }

  /**
   * 以最新 REST 版本记录人工结论。最小 mutation 响应不直接改写状态；409 同样先刷新。
   * @param taskId 需要人工核对的任务 ID。
   * @param resolution 由下一阶段确认对话框产生的明确枚举。
   * @returns 权威快照重新读取后完成；原始安全错误供调用方处理冲突。
   */
  async function resolveManually(
    taskId: string,
    resolution: ManualResolution,
  ): Promise<void> {
    if (mutating.value[taskId]) return
    const owner = epoch
    mutating.value[taskId] = true
    try {
      await loadSnapshot(taskId)
      if (owner !== epoch) return
      const snapshot = snapshots.value[taskId]
      if (!snapshot || snapshotErrors.value[taskId])
        throw new Error('Action snapshot unavailable')
      const result = await resolveAction(
        taskId,
        resolution,
        snapshot.task_version,
      )
      if (owner === epoch) await loadSnapshot(taskId, result.task_version)
    } catch (error) {
      if (owner === epoch) {
        await loadSnapshot(taskId)
        snapshotErrors.value[taskId] = loadError(error)
      }
      throw error
    } finally {
      if (owner === epoch) mutating.value[taskId] = false
    }
  }

  /** 页面卸载或退出会话时清空敏感内存并使在途响应失效；不修改服务器事实。 */
  function clear(): void {
    epoch += 1
    listGeneration += 1
    items.value = []
    snapshots.value = {}
    filters.value = {}
    loading.value = false
    loaded.value = false
    listError.value = null
    snapshotLoading.value = {}
    snapshotErrors.value = {}
    mutating.value = {}
    latestSequences.clear()
    pendingSnapshots.clear()
    refreshAgain.clear()
  }

  return {
    items,
    snapshots,
    filters,
    loading,
    loaded,
    limit,
    offset,
    listError,
    snapshotLoading,
    snapshotErrors,
    mutating,
    refreshList,
    loadSnapshot,
    applyEvent,
    resolveManually,
    clear,
  }
})

/** 公共错误仅保存固定说明与追踪 ID，禁止复制错误详情/正文。 */
function loadError(error: unknown): ActionLoadError {
  return {
    message: '无法刷新操作，请重试。',
    trace_id: error instanceof ProblemError ? error.problem.trace_id : null,
  }
}

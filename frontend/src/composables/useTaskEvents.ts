import {
  onUnmounted,
  ref,
  toValue,
  watch,
  type MaybeRefOrGetter,
  type Ref,
} from 'vue'

import {
  asEventCursor,
  compareEventCursors,
  parseTaskEvent,
  type TaskConnectionState,
  type TaskEvent,
} from '@/api/types'
import { getTask } from '@/api/client'
import { useTasksStore } from '@/stores/tasks'

/** 服务端每 15 秒发送 heartbeat；连续两个周期无任何活动即主动建立新连接。 */
const HEARTBEAT_TIMEOUT_MS = 30_000
const m2EventNames = [
  'action.submitted',
  'approval.invalidated',
  'tool.claimed',
  'tool.oauth_refresh_required',
  'tool.oauth_refresh_confirmed',
  'tool.reconciling',
  'tool.needs_attention',
  'tool.manually_resolved',
] as const

/**
 * 订阅单个任务的可重放 SSE，并在组件销毁或任务切换时释放浏览器连接。
 *
 * @param taskId 任务标识或响应式任务标识；空值时不建立连接。
 * @param onEvent 可选内容无关事件投影回调，仍由本 composable 独占连接。
 * @param onRecovery 重连或心跳发现快照未覆盖流游标后，重取操作列表等 REST 投影；不新建事件流。
 * @returns 可供界面显示的连接状态 ref。
 */
export function useTaskEvents(
  taskId: MaybeRefOrGetter<string | null>,
  onEvent?: (event: TaskEvent) => void,
  onRecovery?: () => void,
): Ref<TaskConnectionState> {
  const tasks = useTasksStore()
  const connectionState = ref<TaskConnectionState>('disconnected')
  let source: EventSource | null = null
  let openedTaskId: string | null = null
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null
  let connectedTaskId: string | null = null

  /** 清除旧流的静默监视器，避免卸载或切换后回调写入已失效任务。 */
  const clearHeartbeatTimeout = (): void => {
    if (heartbeatTimer !== null) clearTimeout(heartbeatTimer)
    heartbeatTimer = null
  }

  /** 关闭现有源并同步独立连接状态，避免旧任务事件写入新页面。 */
  const close = (): void => {
    clearHeartbeatTimeout()
    const closingTaskId = openedTaskId
    source?.close()
    source = null
    openedTaskId = null
    if (closingTaskId) {
      connectionState.value = 'disconnected'
      tasks.setConnectionState(closingTaskId, 'disconnected')
    }
  }

  /** 为当前任务创建 EventSource；浏览器负责协议级重连，错误只改变连接投影。 */
  const open = (nextTaskId: string): void => {
    connectionState.value = 'connecting'
    tasks.setConnectionState(nextTaskId, 'connecting')
    // 审计 ID 在任务之间全局递增，任务专属流中的空洞不是漏事件；服务端按此游标
    // 重放同任务较新的事实，因此使用该任务已见的最大审计 ID。
    const cursor = tasks.latestSequences[nextTaskId]
    const query = cursor === undefined ? '' : `?last_event_id=${cursor}`
    const eventSource = new EventSource(
      `/api/v1/tasks/${encodeURIComponent(nextTaskId)}/events${query}`,
    )
    source = eventSource
    openedTaskId = nextTaskId
    let snapshotPending = false
    let snapshotAgain = false
    let notifyRecovery = false

    /**
     * 同一源最多保留一个快照请求；新持久事件抵达时补取，重复心跳不产生请求风暴。
     * 响应归属和源身份都要匹配，关闭后的异步结果不能污染下一任务。
     */
    const refreshSnapshot = (
      recovery: boolean,
      changedDuringRequest = false,
    ): void => {
      notifyRecovery ||= recovery
      if (snapshotPending) {
        snapshotAgain ||= changedDuringRequest
        return
      }
      snapshotPending = true
      const observedSequence = tasks.latestSequences[nextTaskId]
      void getTask(nextTaskId)
        .then((snapshot) => {
          if (source !== eventSource || snapshot.id !== nextTaskId) return
          tasks.setTaskIfUnchangedSince(snapshot, observedSequence)
        })
        .catch(() => undefined)
        .finally(() => {
          snapshotPending = false
          if (source !== eventSource) return
          if (notifyRecovery) {
            notifyRecovery = false
            onRecovery?.()
          }
          if (snapshotAgain) {
            snapshotAgain = false
            refreshSnapshot(false)
          }
        })
    }
    /**
     * 用所有传输活动续期。若代理或网络静默截断连接而未触发 onerror，主动关闭并
     * 以已知持久游标重开，PostgreSQL 重放负责恢复遗漏事件。
     */
    const armHeartbeatTimeout = (): void => {
      clearHeartbeatTimeout()
      heartbeatTimer = setTimeout(() => {
        if (source !== eventSource) return
        connectionState.value = 'reconnecting'
        tasks.setConnectionState(nextTaskId, 'reconnecting')
        eventSource.close()
        source = null
        openedTaskId = null
        open(nextTaskId)
      }, HEARTBEAT_TIMEOUT_MS)
    }
    armHeartbeatTimeout()
    /**
     * 将监听器绑定到创建它的 EventSource，防止浏览器在 close 后排队的旧回调污染新任务。
     *
     * @param message EventSource 交付的原始事件。
     * @returns 无返回值；已失效连接的消息直接忽略。
     */
    const handleEvent = (message: MessageEvent<string>): void => {
      if (source !== eventSource) return
      // heartbeat 的 {} 不符合持久 TaskEvent，但其抵达仍证明当前传输链路存活，
      // 必须在解析前续期，且已由上方身份判断隔离关闭流的滞后回调。
      armHeartbeatTimeout()
      const event = parseTaskEvent(message.data)
      if (!event) {
        // 原生 EventSource 没有未知 named event 的 wildcard；随后不带 id 的 heartbeat
        // 仍保留浏览器 lastEventId。已知非终态事件可能先推进 latestSequences，却没有覆盖
        // 未知事件的完整事实；恢复必须比较 snapshotCursors，不能把“已见”当作“已恢复”。
        // 这里不判断数字跳号；同任务审计 ID 可以稀疏，已覆盖的重复心跳不再触发读取。
        const wireCursor = asEventCursor(message.lastEventId)
        const snapshotCursor = tasks.snapshotCursors[nextTaskId] ?? '0'
        if (
          wireCursor !== null &&
          compareEventCursors(wireCursor, snapshotCursor) > 0
        )
          refreshSnapshot(true)
        return
      }
      if (event.task_id !== nextTaskId) return
      connectionState.value = 'connected'
      tasks.setConnectionState(event.task_id, 'connected')
      const previousSequence = tasks.latestSequences[nextTaskId]
      tasks.applyEvent(event)
      const isNew =
        previousSequence === undefined ||
        compareEventCursors(event.sequence, previousSequence) > 0
      if (
        isNew &&
        ((event.event === 'task.status_changed' &&
          requiresSnapshot(event.payload.status)) ||
          m2EventNames.some((name) => name === event.event))
      ) {
        // 终态事件可能在代理断线边缘只携带状态；立即以 PostgreSQL 快照对账，补齐步骤、
        // 错误码与最终游标，同时用事件前后的 sequence 防止慢响应回退新事件。
        refreshSnapshot(false, true)
      }
      onEvent?.(event)
    }
    eventSource.onopen = () => {
      if (source !== eventSource) return
      armHeartbeatTimeout()
      connectionState.value = 'connected'
      tasks.setConnectionState(nextTaskId, 'connected')
      if (connectedTaskId === nextTaskId) onRecovery?.()
      connectedTaskId = nextTaskId
    }
    eventSource.onerror = () => {
      if (source !== eventSource) return
      // EventSource 自动重连；不可把短暂网络错误映射为任务业务失败。
      connectionState.value = 'reconnecting'
      tasks.setConnectionState(nextTaskId, 'reconnecting')
    }
    for (const eventName of [
      'task.snapshot',
      'task.status_changed',
      'step.started',
      'step.completed',
      'step.failed',
      'approval.required',
      'approval.resolved',
      'brief.ready',
      'assistant.delta',
      'heartbeat',
      ...m2EventNames,
    ]) {
      eventSource.addEventListener(eventName, handleEvent as EventListener)
    }
  }

  watch(
    () => toValue(taskId),
    (nextTaskId) => {
      close()
      connectedTaskId = null
      if (nextTaskId) open(nextTaskId)
    },
    { immediate: true },
  )
  onUnmounted(close)
  return connectionState
}

/** 终态和人工处理状态需要完整持久字段，不能只展示临时状态变化。 */
function requiresSnapshot(value: unknown): boolean {
  return (
    value === 'succeeded' ||
    value === 'failed' ||
    value === 'cancelled' ||
    value === 'needs_attention'
  )
}

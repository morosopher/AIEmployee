import {
  onUnmounted,
  ref,
  toValue,
  watch,
  type MaybeRefOrGetter,
  type Ref,
} from 'vue'

import { parseTaskEvent, type TaskConnectionState } from '@/api/types'
import { useTasksStore } from '@/stores/tasks'

/** 服务端每 15 秒发送 heartbeat；连续两个周期无任何活动即主动建立新连接。 */
const HEARTBEAT_TIMEOUT_MS = 30_000

/**
 * 订阅单个任务的可重放 SSE，并在组件销毁或任务切换时释放浏览器连接。
 *
 * @param taskId 任务标识或响应式任务标识；空值时不建立连接。
 * @returns 可供界面显示的连接状态 ref。
 */
export function useTaskEvents(
  taskId: MaybeRefOrGetter<string | null>,
): Ref<TaskConnectionState> {
  const tasks = useTasksStore()
  const connectionState = ref<TaskConnectionState>('disconnected')
  let source: EventSource | null = null
  let openedTaskId: string | null = null
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null

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
      const event = parseTaskEvent(message.data)
      if (!event) return
      armHeartbeatTimeout()
      connectionState.value = 'connected'
      tasks.setConnectionState(event.task_id, 'connected')
      tasks.applyEvent(event)
    }
    eventSource.onopen = () => {
      if (source !== eventSource) return
      armHeartbeatTimeout()
      connectionState.value = 'connected'
      tasks.setConnectionState(nextTaskId, 'connected')
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
    ]) {
      eventSource.addEventListener(eventName, handleEvent as EventListener)
    }
  }

  watch(
    () => toValue(taskId),
    (nextTaskId) => {
      close()
      if (nextTaskId) open(nextTaskId)
    },
    { immediate: true },
  )
  onUnmounted(close)
  return connectionState
}

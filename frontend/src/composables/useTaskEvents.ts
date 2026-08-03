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

  /** 关闭现有源并同步独立连接状态，避免旧任务事件写入新页面。 */
  const close = (): void => {
    const closingTaskId = openedTaskId
    source?.close()
    source = null
    openedTaskId = null
    if (closingTaskId) {
      connectionState.value = 'disconnected'
      tasks.setConnectionState(closingTaskId, 'disconnected')
    }
  }

  /** 对每条安全解析的事件更新任务投影，并将传输状态置为已连接。 */
  const handleEvent = (message: MessageEvent<string>): void => {
    const event = parseTaskEvent(message.data)
    if (!event) return
    connectionState.value = 'connected'
    tasks.setConnectionState(event.task_id, 'connected')
    tasks.applyEvent(event)
  }

  /** 为当前任务创建 EventSource；浏览器负责协议级重连，错误只改变连接投影。 */
  const open = (nextTaskId: string): void => {
    connectionState.value = 'connecting'
    tasks.setConnectionState(nextTaskId, 'connecting')
    const cursor = tasks.latestSequences[nextTaskId]
    const query =
      cursor !== undefined && Number.isSafeInteger(cursor) && cursor >= 0
        ? `?last_event_id=${cursor}`
        : ''
    const eventSource = new EventSource(
      `/api/v1/tasks/${encodeURIComponent(nextTaskId)}/events${query}`,
    )
    source = eventSource
    openedTaskId = nextTaskId
    eventSource.onopen = () => {
      if (source !== eventSource) return
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

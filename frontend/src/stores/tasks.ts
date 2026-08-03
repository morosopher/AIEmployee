import { defineStore } from 'pinia'

import {
  asJsonObject,
  asTaskStatus,
  parseTaskSnapshot,
  parseTaskStep,
  type JsonObject,
  type TaskConnectionState,
  type TaskEvent,
  type TaskSnapshot,
  type TaskStep,
} from '@/api/types'

/** 允许服务端协作式取消的非终态；最终合法性仍由服务端状态机决定。 */
export const cancellableTaskStatuses = new Set([
  'created',
  'queued',
  'running',
  'waiting_approval',
])

/** 维护可恢复任务投影与独立 SSE 连接信息的 Pinia Store。 */
export const useTasksStore = defineStore('tasks', {
  state: () => ({
    tasks: {} as Record<string, TaskSnapshot>,
    connections: {} as Record<string, TaskConnectionState>,
    seenSequences: {} as Record<string, Record<number, true>>,
    snapshotCursors: {} as Record<string, number>,
    latestSequences: {} as Record<string, number>,
    projectedStatusSequences: {} as Record<string, number>,
    projectedStepSequences: {} as Record<string, Record<string, number>>,
  }),
  actions: {
    /**
     * 写入 REST 或 retry 返回的权威任务快照，并建立持久事件重放基线。
     *
     * @param snapshot 服务端验证后的任务快照。
     * @returns 无返回值；状态变化由 Pinia 响应式传播。
     */
    setTask(snapshot: TaskSnapshot): void {
      this.tasks[snapshot.id] = {
        ...snapshot,
        steps: orderSteps(snapshot.steps),
      }
      // 快照已包含此游标之前的耐久状态，旧 SSE 重放不得倒退任务或步骤投影。
      this.snapshotCursors[snapshot.id] = snapshot.event_cursor
      this.latestSequences[snapshot.id] = snapshot.event_cursor
      this.seenSequences[snapshot.id] = { [snapshot.event_cursor]: true }
      this.projectedStatusSequences[snapshot.id] = snapshot.event_cursor
      this.projectedStepSequences[snapshot.id] = Object.fromEntries(
        snapshot.steps.map((step) => [step.id, snapshot.event_cursor]),
      )
    },
    /**
     * 仅当 REST 请求期间没有抵达更晚的耐久事件时写入快照，避免初始加载回退 SSE 投影。
     *
     * @param snapshot 服务端验证后的任务快照。
     * @param observedSequence 发起 REST 请求前该任务已知的最高事件序列。
     * @returns 是否已写入快照；序列变化时返回 false。
     */
    setTaskIfUnchangedSince(
      snapshot: TaskSnapshot,
      observedSequence: number,
    ): boolean {
      if ((this.latestSequences[snapshot.id] ?? -1) !== observedSequence)
        return false
      this.setTask(snapshot)
      return true
    },
    /**
     * 单独更新连接展示状态，绝不由断线修改任务持久状态。
     *
     * @param taskId 对应 SSE 的任务标识。
     * @param state 当前浏览器连接状态。
     * @returns 无返回值。
     */
    setConnectionState(taskId: string, state: TaskConnectionState): void {
      this.connections[taskId] = state
    },
    /**
     * 确定性归并一条 SSE 事件：按 sequence 去重，允许乱序步骤并对缺口快照优先。
     *
     * @param event 已在 SSE 边界解析的事件。
     * @returns 无返回值；未知事件保留游标但不擅自推断业务状态。
     */
    applyEvent(event: TaskEvent): void {
      const cursor = this.snapshotCursors[event.task_id] ?? -1
      if (event.event === 'task.snapshot') {
        this.applySnapshotEvent(event)
        return
      }
      if (
        event.sequence <= cursor ||
        this.seenSequences[event.task_id]?.[event.sequence]
      )
        return
      const seen = this.seenSequences[event.task_id] ?? {}
      seen[event.sequence] = true
      this.seenSequences[event.task_id] = seen
      this.latestSequences[event.task_id] = Math.max(
        this.latestSequences[event.task_id] ?? -1,
        event.sequence,
      )

      if (event.event === 'task.status_changed') {
        this.applyStatusEvent(event)
      }
      if (
        event.event === 'step.started' ||
        event.event === 'step.completed' ||
        event.event === 'step.failed'
      ) {
        this.applyStepEvent(event)
      }
    },
    /**
     * 以服务端重放间隙快照覆盖局部投影，并拒绝会回退已知序列的迟到快照。
     *
     * @param event `task.snapshot` SSE 事件。
     * @returns 无返回值；无效 payload 被安全忽略。
     */
    applySnapshotEvent(event: TaskEvent): void {
      try {
        if (event.sequence <= (this.latestSequences[event.task_id] ?? -1))
          return
        const snapshot = parseTaskSnapshot(event.payload)
        if (snapshot.id !== event.task_id) return
        this.setTask(snapshot)
        this.snapshotCursors[event.task_id] = event.sequence
        this.seenSequences[event.task_id] = { [event.sequence]: true }
        this.latestSequences[event.task_id] = event.sequence
        this.projectedStatusSequences[event.task_id] = event.sequence
        this.projectedStepSequences[event.task_id] = Object.fromEntries(
          snapshot.steps.map((step) => [step.id, event.sequence]),
        )
      } catch {
        // 错误的临时传输数据不能清空既有可恢复投影。
      }
    },
    /**
     * 归并服务端显式状态转换；较早事件不得回退已投影状态，缺少合法值时不猜测状态机结果。
     *
     * @param event `task.status_changed` SSE 事件。
     * @returns 无返回值。
     */
    applyStatusEvent(event: TaskEvent): void {
      if (
        event.sequence < (this.projectedStatusSequences[event.task_id] ?? -1)
      )
        return
      const status = asTaskStatus(event.payload.status)
      if (!status) return
      const task = this.tasks[event.task_id] ?? emptyTask(event.task_id, status)
      task.status = status
      if (typeof event.payload.error_code === 'string')
        task.error_code = event.payload.error_code
      this.tasks[event.task_id] = task
      this.projectedStatusSequences[event.task_id] = event.sequence
    },
    /**
     * 归并步骤生命周期并按耐久 sequence 排序，乱序抵达不影响最终显示。
     *
     * @param event 步骤相关 SSE 事件。
     * @returns 无返回值；缺少安全步骤字段的事件被忽略。
     */
    applyStepEvent(event: TaskEvent): void {
      if (!event.step_id) return
      const existingTask =
        this.tasks[event.task_id] ?? emptyTask(event.task_id, 'running')
      const existingStep = existingTask.steps.find(
        (step) => step.id === event.step_id,
      )
      if (
        event.sequence <
        (this.projectedStepSequences[event.task_id]?.[event.step_id] ?? -1)
      )
        return
      const nextStep = mergeStep(existingStep, event)
      if (!nextStep) return
      const steps = existingStep
        ? existingTask.steps.map((step) =>
            step.id === nextStep.id ? nextStep : step,
          )
        : [...existingTask.steps, nextStep]
      this.tasks[event.task_id] = { ...existingTask, steps: orderSteps(steps) }
      const projectedSteps = this.projectedStepSequences[event.task_id] ?? {}
      projectedSteps[event.step_id] = event.sequence
      this.projectedStepSequences[event.task_id] = projectedSteps
    },
  },
})

/**
 * 为只有事件、尚无 REST 快照的情况创建最小投影；下一份快照会完整覆盖它。
 *
 * @param taskId SSE 中的任务标识。
 * @param status 事件明确给出的状态。
 * @returns 最小而可展示的任务对象。
 */
function emptyTask(
  taskId: string,
  status: TaskSnapshot['status'],
): TaskSnapshot {
  return {
    id: taskId,
    kind: 'unknown',
    status,
    retry_of_task_id: null,
    error_code: null,
    event_cursor: 0,
    steps: [],
  }
}

/**
 * 合并一个步骤事件；服务端 payload 的显式字段优先，事件类型补足生命周期状态。
 *
 * @param existing 已知步骤，可能尚不存在。
 * @param event 当前耐久步骤事件。
 * @returns 更新后的步骤，或在缺少必要名称/顺序时返回空值。
 */
function mergeStep(
  existing: TaskStep | undefined,
  event: TaskEvent,
): TaskStep | null {
  const payload = event.payload
  const fromPayload = stepFromPayload(payload)
  const name =
    fromPayload?.name ??
    (typeof payload.name === 'string' ? payload.name : null) ??
    existing?.name
  const sequence =
    fromPayload?.sequence ??
    (typeof payload.sequence === 'number' ? payload.sequence : null) ??
    existing?.sequence ??
    event.sequence
  if (!name) return null
  const status =
    fromPayload?.status ??
    statusForStepEvent(event.event) ??
    existing?.status ??
    'started'
  const base: TaskStep = {
    id: event.step_id ?? existing?.id ?? '',
    sequence,
    name,
    status,
    output_summary:
      fromPayload?.output_summary ??
      existing?.output_summary ??
      asJsonObject(payload.output_summary),
    error_code:
      fromPayload?.error_code ??
      existing?.error_code ??
      (typeof payload.error_code === 'string' ? payload.error_code : null),
    started_at: existing?.started_at,
    finished_at: existing?.finished_at,
  }
  if (event.event === 'step.started') base.started_at = event.occurred_at
  if (event.event === 'step.completed' || event.event === 'step.failed')
    base.finished_at = event.occurred_at
  return base
}

/**
 * 从允许的 payload 形态抽取完整步骤，兼容事件和未来字段扩展。
 *
 * @param payload 事件公开 JSON。
 * @returns 已验证的步骤，或空值。
 */
function stepFromPayload(payload: JsonObject): TaskStep | null {
  const candidate = asJsonObject(payload.step) ?? payload
  try {
    return parseTaskStep(candidate)
  } catch {
    return null
  }
}

/**
 * 把事件名映射为展示状态，避免页面依据英文事件名散落判断。
 *
 * @param eventName 标准步骤事件名。
 * @returns 显示状态或空值。
 */
function statusForStepEvent(eventName: string): string | null {
  if (eventName === 'step.started') return 'started'
  if (eventName === 'step.completed') return 'completed'
  if (eventName === 'step.failed') return 'failed'
  return null
}

/**
 * 以服务端 sequence 进行稳定排序，保证乱序到达后的时间线可预测。
 *
 * @param steps 任意顺序的步骤集合。
 * @returns 新的升序步骤数组，不修改调用方引用。
 */
function orderSteps(steps: readonly TaskStep[]): TaskStep[] {
  return [...steps].sort((left, right) => left.sequence - right.sequence)
}

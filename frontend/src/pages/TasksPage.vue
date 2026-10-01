<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  isNavigationFailure,
  NavigationFailureType,
  RouterLink,
  useRoute,
  useRouter,
} from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import StatusTag from '@/components/StatusTag.vue'
import TaskHistoryList from '@/components/TaskHistoryList.vue'
import { useAuthStore } from '@/stores/auth'
import { useTaskHistory } from '@/features/tasks/useTaskHistory'
import { historyRouteQuery } from '@/features/tasks/historyRoute'

import { cancelTask, getTask, ProblemError, retryTask } from '@/api/client'
import TaskTimeline from '@/components/TaskTimeline.vue'
import { useTaskEvents } from '@/composables/useTaskEvents'
import { cancellableTaskStatuses, useTasksStore } from '@/stores/tasks'
import { focusedEditorPath } from '@/features/actions/editorLinks'

const route = useRoute()
const router = useRouter()
const tasks = useTasksStore()
const auth = useAuthStore()
const history = useTaskHistory({
  timezone: computed(() => auth.user?.timezone ?? null),
})
const historyNavigationError = ref<string | null>(null)
const historyTitle = ref<HTMLElement | null>(null)
const detailTitle = ref<HTMLElement | null>(null)
/** 当前页面已收到404的身份不得再凭旧缓存恢复；只由该身份新的成功GET解除。 */
const unavailableTaskIds = ref(new Set<string>())
/** Pinia会跨页面挂载保留投影；本次挂载先由成功GET确认存在，再消费原Store/SSE更新。 */
const confirmedTaskIds = ref(new Set<string>())
/** API 错误只选择固定文案与受控追踪标识，不显示 detail、URL 或原始游标。 */
const historyError = computed(() => {
  if (historyNavigationError.value) return historyNavigationError.value
  const cause = history.error.value
  if (!cause) return null
  if (!(cause instanceof ProblemError)) return cause.message
  const message =
    cause.problem.error_code === 'task_history_cursor_invalid'
      ? '分页凭据已失效，请重新加载。'
      : cause.problem.error_code === 'task_history_filter_invalid'
        ? '筛选条件无效，请重新选择。'
        : '无法读取任务历史，请重试。'
  return cause.problem.trace_id
    ? `${message}追踪编号：${cause.problem.trace_id}`
    : message
})
/**
 * 捕获状态层导航失败并归还列表控件焦点，不把原始 query/cursor 写入 console。
 * @param action 用户触发的只读筛选、翻页或刷新；动作完成通知也可使用此安全出口。
 * @returns 请求落定且内联错误/焦点恢复后完成；不影响详情的独立状态或动作结果。
 */
async function runHistory(action: () => Promise<void>): Promise<void> {
  historyNavigationError.value = null
  const focused = document.activeElement
  const trigger = focused instanceof HTMLElement && focused.closest('[aria-label="任务历史列表"]') ? focused : null
  try {
    await action()
  } catch {
    historyNavigationError.value = '任务历史导航未完成，请重试。'
  } finally {
    await nextTick()
    // 仅归还被加载态禁用的列表控件焦点；用户已主动移到别处时不抢焦点。
    if (trigger?.isConnected && (document.activeElement === trigger || document.activeElement === document.body)) {
      if (trigger.matches(':disabled')) historyTitle.value?.focus()
      else trigger.focus()
    }
  }
}
/**
 * 构造保留公开筛选和分页的站内任务地址，未知 query 不传播。
 * @param id 完整任务身份；null 表示返回列表并去掉 task_id。
 * @returns Vue Router 位置对象，不携带正文或结果载荷。
 */
function taskLocation(id: string | null) {
  const query = historyRouteQuery(
    route.query,
    history.filters.value,
    history.cursor.value,
  )
  if (id === null) delete query.task_id
  else query.task_id = id
  return { path: '/tasks', query }
}
/** @param id 完整任务身份。@returns 由当前路由解析的站内链接，交给纯展示组件。 */
function taskHref(id: string): string {
  return router.resolve(taskLocation(id)).href
}
/** 选择/返回/替换都接住已拒绝或被取消的导航；焦点仅随用户动作移动。 */
async function navigateTask(id: string | null, replace = false): Promise<void> {
  historyNavigationError.value = null
  try {
    const failure = await (replace
      ? router.replace(taskLocation(id))
      : router.push(taskLocation(id)))
    if (
      failure &&
      !isNavigationFailure(failure, NavigationFailureType.duplicated)
    )
      throw failure
    await nextTick()
    if (id === null) historyTitle.value?.focus()
    else detailTitle.value?.focus()
  } catch {
    historyNavigationError.value = '任务历史导航未完成，请重试。'
  }
}
const loading = ref(false)
const error = ref<string | null>(null)
const retryIntentKeys = new Map<string, string>()
let loadGeneration = 0
const taskId = computed(() =>
  typeof route.query.task_id === 'string' ? route.query.task_id : null,
)
const task = computed(() =>
  taskId.value && confirmedTaskIds.value.has(taskId.value) && !unavailableTaskIds.value.has(taskId.value)
    ? (tasks.tasks[taskId.value] ?? null)
    : null,
)
const connectionState = useTaskEvents(taskId)
/** 只用受限任务快照构造恢复结果链接，准备回执和临时事件不能伪造完成提案。 */
const restoreEditorUrl = computed(() =>
  task.value?.kind === 'calendar.restore.prepare' &&
  task.value.status === 'succeeded' &&
  task.value.calendar_restore_proposal_id
    ? focusedEditorPath(
        `/calendar/proposals/${task.value.calendar_restore_proposal_id}`,
      )
    : null,
)
const canCancel = computed(
  () => task.value !== null && cancellableTaskStatuses.has(task.value.status),
)

/**
 * 根据路由任务标识恢复 REST 快照；请求期间抵达的 SSE 投影优先，避免旧响应回退状态。
 *
 * @param nextTaskId 当前路由中的任务标识。
 * @returns Promise 在成功、空选择或错误显示后完成。
 */
async function loadTask(nextTaskId: string | null): Promise<void> {
  const generation = ++loadGeneration
  error.value = null
  if (!nextTaskId) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const observedSequence = tasks.latestSequences[nextTaskId]
    const snapshot = await getTask(nextTaskId)
    if (generation === loadGeneration && taskId.value === nextTaskId) {
      tasks.setTaskIfUnchangedSince(snapshot, observedSequence)
      unavailableTaskIds.value.delete(nextTaskId)
      confirmedTaskIds.value.add(nextTaskId)
    }
  } catch (cause) {
    if (generation === loadGeneration) {
      if (cause instanceof ProblemError && cause.problem.status === 404)
        unavailableTaskIds.value.add(nextTaskId)
      error.value = unavailableTaskIds.value.has(nextTaskId)
        ? '任务记录已不可用，请返回任务列表。'
        : '无法加载该任务，请刷新页面后重试。'
    }
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

watch(
  taskId,
  (nextTaskId, previousTaskId) => {
    if (nextTaskId !== previousTaskId) retryIntentKeys.clear()
    void loadTask(nextTaskId)
  },
  { immediate: true },
)

/**
 * 仅对当前服务端允许取消的状态发起请求，并以返回快照覆盖本地投影。
 *
 * @returns Promise 在请求结束后完成。
 */
async function cancelCurrentTask(): Promise<void> {
  if (!task.value || !canCancel.value) return
  error.value = null
  try {
    tasks.setTask(await cancelTask(task.value.id))
    void runHistory(history.refreshCurrent)
  } catch {
    error.value = '取消请求未完成，请稍后重试。'
  }
}

/**
 * 请求服务端创建 replacement；幂等键只用于本次重试请求，不包含用户秘密。
 *
 * @param failedTaskId 当前失败任务标识。
 * @returns 新 replacement 的权威快照。
 */
async function retryFailedTask(failedTaskId: string) {
  const idempotencyKey =
    retryIntentKeys.get(failedTaskId) ??
    `task-retry:${failedTaskId}:${crypto.randomUUID()}`
  retryIntentKeys.set(failedTaskId, idempotencyKey)
  try {
    const replacement = await retryTask(failedTaskId, idempotencyKey)
    retryIntentKeys.delete(failedTaskId)
    // 列表是只读旁路；慢请求不得阻塞原 replacement 跟随。
    void runHistory(history.refreshCurrent)
    return replacement
  } catch (retryError) {
    // 服务器已明确拒绝时该意图未执行；运输层失败仍可能已创建任务，必须保留同键安全重放。
    if (retryError instanceof ProblemError) retryIntentKeys.delete(failedTaskId)
    throw retryError
  }
}

/**
 * 跟随服务端返回的 replacement；重试来源始终由 replacement 快照提供。
 *
 * @param replacementTaskId 新任务标识。
 * @returns 无返回值；路由变化会自动重建 SSE 订阅。
 */
function followReplacement(replacementTaskId: string): void {
  void navigateTask(replacementTaskId, true)
}
</script>

<template>
  <section
    aria-labelledby="tasks-title"
    class="min-w-0"
  >
    <h1
      id="tasks-title"
      class="mb-4 text-2xl font-semibold"
    >
      任务中心
    </h1>
    <div class="grid min-w-0 grid-cols-1 gap-6 xl:grid-cols-2">
      <div class="min-w-0">
        <h2
          ref="historyTitle"
          tabindex="-1"
          class="mb-4 rounded text-xl font-semibold focus-visible:outline-2"
        >
          任务历史
        </h2>
        <TaskHistoryList
          :page="history.page.value"
          :filters="history.filters.value"
          :loading="history.loading.value"
          :error="historyError"
          :new-task-hint="history.hasNewTasks.value"
          :task-href="taskHref"
          @filter="runHistory(() => history.setFilters($event))"
          @next="runHistory(history.nextPage)"
          @previous="runHistory(history.previousPage)"
          @refresh="runHistory(history.refreshFirst)"
          @select="navigateTask($event)"
        />
      </div>
      <section
        aria-label="任务详情"
        class="flex min-w-0 flex-col gap-3 rounded-lg border border-surface-200 p-4"
      >
        <h2
          ref="detailTitle"
          tabindex="-1"
          class="rounded text-xl font-semibold focus-visible:outline-2"
        >
          任务详情
        </h2>
        <p v-if="!taskId">
          请从任务历史选择一个任务。
        </p>
        <template v-else>
          <Button
            label="返回任务列表"
            severity="secondary"
            class="self-start"
            @click="navigateTask(null)"
          />
          <Message
            role="status"
            aria-live="polite"
            severity="info"
          >
            实时连接：{{ connectionState }}
          </Message>
          <Message
            v-if="loading"
            role="status"
            aria-live="polite"
            severity="info"
          >
            正在恢复任务快照…
          </Message>
          <Message
            v-if="error"
            role="alert"
            severity="error"
          >
            {{
              error
            }}
          </Message>
          <template v-if="task">
            <h3 class="break-words font-semibold">
              {{ task.kind }}
            </h3>
            <p>
              <span>当前状态：{{ task.status }}</span>
              <StatusTag
                kind="task"
                :value="task.status"
              />
            </p>
            <template v-if="task.kind === 'calendar.restore.prepare'">
              <Message
                role="status"
                aria-live="polite"
                severity="info"
              >
                准备完成后仍需核对并提交新的审批，日程尚未因此恢复。
              </Message>
              <RouterLink
                v-if="restoreEditorUrl"
                :to="restoreEditorUrl"
                data-testid="restore-result"
                class="text-primary underline"
              >
                打开恢复提案
              </RouterLink>
              <Message
                v-else-if="task.status === 'succeeded'"
                role="alert"
                severity="error"
              >
                恢复提案结果已不可用，请返回操作中心核对原修改和保留期。
              </Message>
            </template>
            <Button
              v-if="canCancel"
              label="取消任务"
              severity="secondary"
              class="self-start"
              @click="cancelCurrentTask"
            />
            <p
              v-if="task.retry_of_task_id"
              class="break-all"
            >
              此任务重试自：{{ task.retry_of_task_id }}
            </p>
          </template>
        </template>
        <TaskTimeline
          :task="task"
          :retry="retryFailedTask"
          :follow="followReplacement"
        />
      </section>
    </div>
  </section>
</template>

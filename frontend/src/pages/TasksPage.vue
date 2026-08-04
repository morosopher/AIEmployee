<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { cancelTask, getTask, ProblemError, retryTask } from '@/api/client'
import TaskTimeline from '@/components/TaskTimeline.vue'
import { useTaskEvents } from '@/composables/useTaskEvents'
import { cancellableTaskStatuses, useTasksStore } from '@/stores/tasks'

const route = useRoute()
const router = useRouter()
const tasks = useTasksStore()
const loading = ref(false)
const error = ref<string | null>(null)
const retryIntentKeys = new Map<string, string>()
let loadGeneration = 0
const taskId = computed(() =>
  typeof route.query.task_id === 'string' ? route.query.task_id : null,
)
const task = computed(() =>
  taskId.value ? (tasks.tasks[taskId.value] ?? null) : null,
)
const connectionState = useTaskEvents(taskId)
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
  if (!nextTaskId) return
  loading.value = true
  try {
    const observedSequence = tasks.latestSequences[nextTaskId]
    const snapshot = await getTask(nextTaskId)
    if (generation === loadGeneration && taskId.value === nextTaskId)
      tasks.setTaskIfUnchangedSince(snapshot, observedSequence)
  } catch {
    if (generation === loadGeneration)
      error.value = '无法加载该任务，请刷新页面后重试。'
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
  void router.replace({ path: '/tasks', query: { task_id: replacementTaskId } })
}
</script>

<template>
  <main class="tasks-page">
    <section aria-labelledby="tasks-title">
      <h1 id="tasks-title">
        任务中心
      </h1>
      <p v-if="!taskId">
        请从任务历史选择一个任务。
      </p>
      <template v-else>
        <p role="status">
          实时连接：{{ connectionState }}
        </p>
        <p
          v-if="loading"
          role="status"
        >
          正在恢复任务快照…
        </p>
        <p
          v-if="error"
          role="alert"
        >
          {{ error }}
        </p>
        <template v-if="task">
          <h2>{{ task.kind }}</h2>
          <p>当前状态：{{ task.status }}</p>
          <button
            v-if="canCancel"
            type="button"
            @click="cancelCurrentTask"
          >
            取消任务
          </button>
          <p v-if="task.retry_of_task_id">
            此任务重试自：{{ task.retry_of_task_id }}
          </p>
        </template>
      </template>
    </section>
    <TaskTimeline
      :task="task"
      :retry="retryFailedTask"
      :follow="followReplacement"
    />
  </main>
</template>

<style scoped>
.tasks-page {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(20rem, 30rem);
  min-height: 100vh;
}
section {
  padding: 1.5rem;
}
@media (max-width: 700px) {
  .tasks-page {
    grid-template-columns: 1fr;
  }
}
</style>

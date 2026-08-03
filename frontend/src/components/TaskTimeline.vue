<script setup lang="ts">
import { computed, ref } from 'vue'

import type { TaskSnapshot, TaskStep } from '@/api/types'

/** 任务时间线的输入契约；所有内容均以纯文本形式显示。 */
interface Props {
  task: TaskSnapshot | null
  retry: (taskId: string) => Promise<TaskSnapshot>
  follow: (taskId: string, originalTaskId: string) => void
}

const props = defineProps<Props>()
const retrying = ref(false)
const retryError = ref<string | null>(null)

/** 当前任务是否允许创建 replacement；原失败任务本身永远不被前端改写。 */
const canRetry = computed(
  () => props.task?.status === 'failed' && !retrying.value,
)

/**
 * 请求服务端创建 replacement，并把页面跟随到返回的新任务。
 *
 * @returns Promise 在请求结束时完成；失败仅呈现可恢复的用户提示。
 */
async function retryTask(): Promise<void> {
  if (!props.task || !canRetry.value) return
  retrying.value = true
  retryError.value = null
  try {
    const replacement = await props.retry(props.task.id)
    props.follow(replacement.id, props.task.id)
  } catch {
    retryError.value = '重试请求未完成，请稍后再试。'
  } finally {
    retrying.value = false
  }
}

/**
 * 将可选摘要转换为纯文本 JSON，模板插值不会解析为 HTML。
 *
 * @param summary 服务端提供的结构化摘要。
 * @returns 供 `<pre>` 显示的稳定文本。
 */
function summaryText(summary: TaskStep['output_summary']): string {
  return summary ? JSON.stringify(summary, null, 2) : '暂无摘要'
}

/**
 * 计算已知步骤耗时；缺少两个事件时间时明确显示未知而不臆测。
 *
 * @param step 已归并的步骤。
 * @returns 简短时长文本。
 */
function durationText(step: TaskStep): string {
  if (!step.started_at || !step.finished_at) return '耗时未知'
  const duration = Date.parse(step.finished_at) - Date.parse(step.started_at)
  return Number.isFinite(duration) && duration >= 0
    ? `耗时 ${Math.round(duration / 1000)} 秒`
    : '耗时未知'
}
</script>

<template>
  <aside
    class="task-timeline"
    aria-label="执行时间线"
  >
    <p
      v-if="!task"
      role="status"
    >
      请选择一个任务查看执行时间线。
    </p>
    <template v-else>
      <header>
        <h2>执行时间线</h2>
        <p>任务状态：{{ task.status }}</p>
      </header>
      <p
        v-if="task.error_code"
        class="error"
        role="alert"
      >
        错误代码：{{ task.error_code }}
      </p>
      <ol
        v-if="task.steps.length"
        aria-label="任务步骤"
      >
        <li
          v-for="step in task.steps"
          :key="step.id"
        >
          <strong>{{ step.name }}</strong>
          <span>状态：{{ step.status }}</span>
          <span>{{ durationText(step) }}</span>
          <span
            v-if="step.error_code"
            class="error"
          >错误代码：{{ step.error_code }}</span>
          <details>
            <summary>查看摘要</summary>
            <pre>{{ summaryText(step.output_summary) }}</pre>
          </details>
        </li>
      </ol>
      <p v-else>
        暂时没有可展示的步骤。
      </p>
      <button
        v-if="canRetry"
        type="button"
        @click="retryTask"
      >
        重试任务
      </button>
      <p
        v-if="retryError"
        class="error"
        role="alert"
      >
        {{ retryError }}
      </p>
    </template>
  </aside>
</template>

<style scoped>
.task-timeline {
  padding: 1rem;
  border-left: 1px solid #d7dce5;
}
ol {
  padding-left: 1.25rem;
}
li {
  display: grid;
  gap: 0.25rem;
  margin-bottom: 1rem;
}
pre {
  overflow: auto;
  white-space: pre-wrap;
}
.error {
  color: #a61b1b;
}
</style>

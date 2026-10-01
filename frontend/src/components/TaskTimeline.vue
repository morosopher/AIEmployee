<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Timeline from 'primevue/timeline'
import StatusTag from './StatusTag.vue'

import { ProblemError } from '@/api/client'
import type { TaskSnapshot, TaskStep } from '@/api/types'

/** 任务时间线的输入契约；所有内容均以纯文本形式显示。 */
interface Props {
  task: TaskSnapshot | null
  retry: (taskId: string) => Promise<TaskSnapshot>
  follow: (taskId: string) => void
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
    props.follow(replacement.id)
  } catch (error) {
    retryError.value =
      error instanceof ProblemError
        ? '重试被服务器拒绝，请处理当前任务状态后再试。'
        : '重试结果未知；再次点击会安全重放同一请求。'
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
    class="min-w-0"
    aria-label="执行时间线"
  >
    <Message
      v-if="!task"
      role="status"
      aria-live="polite"
      severity="info"
    >
      请选择一个任务查看执行时间线。
    </Message>
    <template v-else>
      <header>
        <h2>执行时间线</h2>
        <p>
          <span>任务状态：{{ task.status }}</span>
          <StatusTag
            kind="task"
            :value="task.status"
          />
        </p>
      </header>
      <Message
        v-if="task.error_code"
        severity="error"
        role="alert"
      >
        错误代码：{{ task.error_code }}
      </Message>
      <!-- 仅使用服务端确认的配置错误码提供恢复指引，不显示供应商原始响应。 -->
      <p v-if="task.error_code === 'google_api_not_enabled'">
        请在 Google Cloud 中为当前 OAuth 应用所属项目启用对应的 Gmail API 或
        Google Calendar API，启用后重试同步。
      </p>
      <Timeline
        v-if="task.steps.length"
        :value="task.steps"
        data-key="id"
        aria-label="任务步骤"
        :pt="{
          eventOpposite: { class: 'hidden' },
          eventContent: { class: 'min-w-0 pb-4' },
        }"
      >
        <template #content="{ item: step }: { item: TaskStep }">
          <div class="flex min-w-0 flex-col gap-1">
            <strong>{{ step.name }}</strong>
            <span>状态：{{ step.status }}</span>
            <span>{{ durationText(step) }}</span>
            <span
              v-if="step.error_code"
              class="text-red-700"
            >错误代码：{{ step.error_code }}</span>
            <details>
              <summary>查看摘要</summary>
              <pre class="overflow-auto whitespace-pre-wrap break-words">{{
                summaryText(step.output_summary)
              }}</pre>
            </details>
          </div>
        </template>
      </Timeline>
      <p v-else>
        暂时没有可展示的步骤。
      </p>
      <Button
        v-if="canRetry"
        label="重试任务"
        @click="retryTask"
      />
      <Message
        v-if="retryError"
        severity="error"
        role="alert"
      >
        {{ retryError }}
      </Message>
    </template>
  </aside>
</template>

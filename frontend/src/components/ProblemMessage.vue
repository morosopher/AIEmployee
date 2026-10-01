<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import type { ProblemError } from '@/api/client'

/** 只接收 API 公开错误与可选恢复动作；不展示 detail、堆栈或未知供应商文本。 */
const props = defineProps<{ problem: ProblemError; actionLabel?: string }>()
/** 恢复动作由页面处理，组件不自动重试，避免重放结果未知的外部写请求。 */
defineEmits<{ action: [] }>()

/** 已知错误码提供安全中文说明，未知码与标题均使用固定兜底，禁止透传错误正文。 */
const description = computed(() => {
  if (props.problem.problem.error_code === 'approval_version_conflict') {
    return '审批版本已变化，请重新加载后再试。'
  }
  return '操作未能完成，请稍后重试。'
})
</script>

<template>
  <!-- Message 自带告警语义，在同一根节点明确 role；不嵌套第二个 live region。 -->
  <Message
    severity="error"
    role="alert"
  >
    <div class="space-y-2">
      <p>{{ description }}</p>
      <p
        v-if="problem.problem.trace_id"
        class="text-sm"
      >
        追踪编号：<span class="break-all font-mono">{{
          problem.problem.trace_id
        }}</span>
      </p>
      <Button
        v-if="actionLabel"
        :label="actionLabel"
        severity="secondary"
        @click="$emit('action')"
      />
    </div>
  </Message>
</template>

<script setup lang="ts">
import Message from 'primevue/message'
import { RouterLink } from 'vue-router'
import type { SystemAlert } from '@/api/system'

/** 外壳已有告警快照的纯展示契约；失败时仍保留最后一份逾期告警。 */
defineProps<{ loading: boolean; error: boolean; alerts: SystemAlert[] }>()
</script>

<template>
  <div class="space-y-2">
    <!-- 覆盖 Message 默认 assertive，保留加载状态的礼貌播报语义。 -->
    <Message
      v-if="loading"
      severity="secondary"
      role="status"
      aria-live="polite"
    >
      正在检查系统告警
    </Message>
    <Message
      v-if="error"
      severity="warn"
      role="alert"
    >
      系统告警暂时无法刷新。
    </Message>
    <Message
      v-if="alerts.length"
      severity="error"
      role="alert"
    >
      <strong>每日简报已逾期</strong>
      <RouterLink
        v-if="alerts[0]?.diagnostic_task_id"
        :to="{
          path: '/tasks',
          query: { task_id: alerts[0].diagnostic_task_id },
        }"
        class="ml-4 underline"
      >
        查看诊断任务
      </RouterLink>
    </Message>
  </div>
</template>

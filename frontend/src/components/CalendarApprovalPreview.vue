<script setup lang="ts">
import type { CalendarApprovalPreview } from '@/api/types'
import { providerLabel } from '@/features/actions/presentation'
import CalendarFieldsComparison from './CalendarFieldsComparison.vue'
import CalendarConflictNotice from './CalendarConflictNotice.vue'

/** 审批只消费冻结账户、日历和前后值，不用编辑态内容覆盖历史。 */
defineProps<{ preview: CalendarApprovalPreview }>()
const operations = {
  create: '创建日程',
  update: '修改日程',
  restore: '恢复日程',
}
</script>
<template>
  <section aria-label="日程审批预览">
    <p>
      {{ operations[preview.operation] }} ·
      {{ providerLabel(preview.provider) }} · {{ preview.account_email }}
    </p>
    <p>目标日历：{{ preview.calendar_name }}</p>
    <CalendarFieldsComparison
      :before="preview.before"
      :after="preview.after"
    />
    <p>
      通知策略：{{
        preview.notification_policy === 'all'
          ? '发送通知给所有参会人'
          : '不发送通知'
      }}
    </p>
    <p v-if="preview.base_etag">
      ETag：{{ preview.base_etag }}
    </p>
    <p>
      {{
        preview.compensation_available
          ? '可以另建恢复提案；恢复仍需重新审批。'
          : '此操作不提供自动撤销。'
      }}
    </p>
    <p
      v-if="
        preview.provider_warnings.includes(
          'google_send_updates_none_external_sync',
        )
      "
      class="warning"
    >
      Google 不发送通知可能影响外部同步，请核对参会人的日历。
    </p>
    <CalendarConflictNotice :conflicts="preview.conflicts" />
  </section>
</template>
<style scoped>
.warning {
  background: #fff5df;
  padding: 0.75rem;
}
</style>

<script setup lang="ts">
import type { MailApprovalPreview } from '@/api/types'
import { providerLabel } from '@/features/actions/presentation'

/** 冻结字段仅按普通文本展示，正文从不进入 Markdown、HTML 或持久存储。 */
defineProps<{ preview: MailApprovalPreview }>()
const modes = { new: '新邮件', reply: '回复', reply_all: '全部回复' }
</script>
<template>
  <section aria-label="邮件审批预览">
    <p>
      {{ providerLabel(preview.provider) }} · {{ preview.account_email }} ·
      {{ modes[preview.mode] }}
    </p>
    <dl>
      <dt>收件人 To</dt>
      <dd>{{ preview.to.join('、') || '无' }}</dd>
      <dt>抄送 CC</dt>
      <dd>{{ preview.cc.join('、') || '无' }}</dd>
      <dt>密送 BCC</dt>
      <dd>{{ preview.bcc.join('、') || '无' }}</dd>
      <dt>主题</dt>
      <dd data-testid="mail-approval-subject">
        {{ preview.subject || '（无主题）' }}
      </dd>
      <dt>纯文本正文</dt>
      <dd class="plain-text">
        {{ preview.body_text }}
      </dd>
    </dl>
    <p class="warning">
      邮件发送后不可撤回。请逐项检查全部收件人和正文。
    </p>
  </section>
</template>
<style scoped>
dl {
  display: grid;
  grid-template-columns: minmax(5rem, auto) minmax(0, 1fr);
  gap: 0.65rem;
}
dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.plain-text {
  white-space: pre-wrap;
}
.warning {
  background: #fff5df;
  padding: 0.75rem;
  border-left: 3px solid #946200;
}
</style>

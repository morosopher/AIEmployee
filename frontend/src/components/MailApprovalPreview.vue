<script setup lang="ts">
import { computed } from 'vue'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import type { MailApprovalPreview } from '@/api/types'
import { providerLabel } from '@/features/actions/presentation'

/** 冻结字段仅按普通文本展示，正文从不进入 Markdown、HTML 或持久存储。 */
const props = defineProps<{ preview: MailApprovalPreview }>()
const modes = { new: '新邮件', reply: '回复', reply_all: '全部回复' }
/** 仅把既有字段映射为只读表格；正文逐字保留，空主题沿用原占位语义。 */
const rows = computed(() => [
  { label: '收件人 To', value: props.preview.to.join('、') || '无' },
  { label: '抄送 CC', value: props.preview.cc.join('、') || '无' },
  { label: '密送 BCC', value: props.preview.bcc.join('、') || '无' },
  { label: '主题', value: props.preview.subject || '（无主题）' },
  { label: '纯文本正文', value: props.preview.body_text },
])
</script>
<template>
  <section
    aria-label="邮件审批预览"
    class="min-w-0 space-y-3"
  >
    <p>
      {{ providerLabel(preview.provider) }} · {{ preview.account_email }} ·
      {{ modes[preview.mode] }}
    </p>
    <div
      class="overflow-x-auto"
      tabindex="0"
      aria-label="邮件冻结载荷滚动区域"
    >
      <DataTable
        :value="rows"
        data-key="label"
        :table-props="{ 'aria-label': '邮件冻结载荷' }"
      >
        <Column
          field="label"
          header="字段"
          :pt="{
            headerCell: { scope: 'col' },
            bodyCell: { role: 'rowheader' },
          }"
        />
        <Column
          field="value"
          header="精确载荷"
          class="whitespace-pre-wrap wrap-anywhere align-top"
          :pt="{ headerCell: { scope: 'col' } }"
        >
          <template #body="{ data }">
            <div>{{ data.value }}</div>
          </template>
        </Column>
      </DataTable>
    </div>
    <!-- 原静态风险提示保持可见；覆盖 Message 默认 alert，避免重复宣告冻结字段。 -->
    <Message
      severity="warn"
      role="note"
      aria-live="off"
    >
      邮件发送后不可撤回。请逐项检查全部收件人和正文。
    </Message>
  </section>
</template>

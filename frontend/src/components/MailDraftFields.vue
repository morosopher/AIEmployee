<script setup lang="ts">
import type { MailDraft } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import type {
  MailEditorForm,
  MailRecipientSummary,
} from '@/features/mail/useMailDraftEditor'
import EditorConnectionSelect from './EditorConnectionSelect.vue'

/** 普通文本表单；v-model 只修改当前输入模型，所有保存与审批由父级显式触发。 */
defineProps<{
  draft: MailDraft
  entries: ConnectionCatalogEntry[]
  disabled: boolean
  recipientSummary: MailRecipientSummary
}>()
const form = defineModel<MailEditorForm>({ required: true })
</script>
<template>
  <div class="editor-fields">
    <div class="wide-field">
      <EditorConnectionSelect
        v-model="form.connection_id"
        :entries="entries"
        capability="mail.send"
        label="发送账户"
        :disabled="disabled || draft.mode !== 'new'"
      />
    </div>
    <p
      v-if="draft.mode !== 'new'"
      class="wide-field"
    >
      回复已绑定来源线程：{{
        draft.source_thread_id
      }}。账户、线程和主题保持绑定。
    </p>
    <label>收件人 To<input
      v-model="form.to"
      aria-label="收件人 To"
      list="local-recipient-suggestions"
      :disabled="disabled"
      autocomplete="off"
    ></label>
    <label>抄送 CC<input
      v-model="form.cc"
      aria-label="抄送 CC"
      list="local-recipient-suggestions"
      :disabled="disabled"
      autocomplete="off"
    ></label>
    <label>密送 BCC<input
      v-model="form.bcc"
      aria-label="密送 BCC"
      list="local-recipient-suggestions"
      :disabled="disabled"
      autocomplete="off"
    ></label>
    <datalist id="local-recipient-suggestions">
      <option
        v-for="address in draft.recipient_suggestions"
        :key="address"
        :value="address"
      />
    </datalist>
    <p class="wide-field">
      多个邮箱用逗号分隔，To、CC、BCC 合计最多 50 位。建议仅来自本地已同步邮件。
    </p>
    <p
      class="wide-field"
      data-testid="recipient-count"
      role="status"
    >
      <template v-if="recipientSummary.count !== null">
        当前收件人数：{{ recipientSummary.count }} 位
      </template>
      <template v-else>
        收件人数待核对。
        <span v-if="recipientSummary.inputCount !== null">当前输入地址：{{ recipientSummary.inputCount }} 项。</span>
        {{ recipientSummary.error }}
      </template>
    </p>
    <label class="wide-field">主题<input
      v-model="form.subject"
      aria-label="主题"
      :disabled="disabled || draft.mode !== 'new'"
      maxlength="998"
    ></label>
    <label class="wide-field">纯文本正文<textarea
      v-model="form.body_text"
      aria-label="纯文本正文"
      rows="12"
      :disabled="disabled"
    />
    </label>
  </div>
</template>

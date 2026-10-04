<script setup lang="ts">
import { ref, watch } from 'vue'
import Form, {
  type FormInstance,
  type FormSubmitEvent,
} from '@primevue/forms/form'
import FormField from '@primevue/forms/formfield'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Textarea from 'primevue/textarea'
import type { MailDraft } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import type {
  MailEditorForm,
  MailRecipientSummary,
} from '@/features/mail/useMailDraftEditor'
import { mailResolver } from '@/features/mail/schema'
import MailDraftFields from './MailDraftFields.vue'
import ProblemMessage from './ProblemMessage.vue'
import EditorRecovery from './EditorRecovery.vue'
import { computed } from 'vue'
import type { ActionRecovery } from '@/features/actions/recovery'
import { problemToFormError } from '@/features/forms/problemFields'

/**
 * 异步展示边界承载 Form/zod/AutoComplete；只转发明确操作，原页面 hook 是唯一请求与状态来源。
 * Live region：原未保存和生成中两个 status、生成失败一个 alert 逐字保留；格式错误最多新增
 * 五个字段 alert。真实 error_code 的表单说明置于同一 EditorRecovery alert，不重复播报追踪号。
 */
const props = defineProps<{
  error: ActionRecovery | null
  creating: boolean
  modelValue: MailEditorForm
  draft: MailDraft
  entries: ConnectionCatalogEntry[]
  recipientSummary: MailRecipientSummary
  dirty: boolean
  busy: boolean
  locked: boolean
  canSubmit: boolean
  generationRunning: boolean
  generationFailed: boolean
  generationConnection: string
}>()
const instruction = defineModel<string>('instruction', { required: true })
const emit = defineEmits<{
  save: []
  submit: []
  generate: []
  reload: []
  newObject: []
}>()
/** 仅标记发起中的按钮以呈现 loading；锁定、幂等与任务终态仍完全来自原 hook。 */
const activeOperation = ref<'save' | 'submit' | 'generate' | null>(null)
const generationLoading = computed(
  () =>
    props.generationRunning ||
    (props.busy && activeOperation.value === 'generate'),
)
watch(
  () => props.busy,
  (busy) => {
    if (!busy) activeOperation.value = null
  },
)
const formApi = ref<FormInstance | null>(null)
const initialValues = ref({ ...props.modelValue })
const formKey = ref(0)
const mappedError = computed(() =>
  props.error?.problem ? problemToFormError(props.error.problem) : null,
)
const recipientNames = ['to', 'cc', 'bcc'] as const
/** 只在权威快照引用变化时重建，409 不替换快照，因此保留原输入及 Form 状态。 */
watch(
  () => props.draft,
  () => {
    initialValues.value = { ...props.modelValue }
    formKey.value += 1
  },
)
/** chips 组件模型是数组，真实 FormField 始终注册原字符串，并包含尚未确认输入。 */
for (const name of recipientNames) {
  watch(
    () => props.modelValue[name],
    (value) => formApi.value?.setFieldValue(name, value),
    { flush: 'sync' },
  )
}
/** @param event Form 的真实 resolver 结果；提交操作再次遵守原 dirty/locked/canSubmit 守卫。 */
function submitForm(event: FormSubmitEvent): void {
  if (!event.valid || props.locked) return
  const button = (event.originalEvent as SubmitEvent).submitter
  if (button instanceof HTMLButtonElement && button.name === 'submit-draft') {
    if (props.canSubmit) {
      activeOperation.value = 'submit'
      emit('submit')
    }
  } else if (props.dirty) {
    activeOperation.value = 'save'
    emit('save')
  }
}
/** 显式生成不经过整张 Form 校验，仍保留原锁定／dirty／指令守卫，不创建新的请求状态机。 */
function generateBody(): void {
  if (props.locked || props.dirty || !instruction.value.trim()) return
  activeOperation.value = 'generate'
  emit('generate')
}
</script>
<template>
  <Form
    :key="formKey"
    ref="formApi"
    v-slot="$form"
    :initial-values="initialValues"
    :resolver="mailResolver"
    :validate-on-value-update="false"
    :validate-on-blur="false"
    aria-label="邮件草稿表单"
    class="space-y-4"
    @submit="submitForm"
  >
    <EditorRecovery
      :error="error"
      :busy="busy || creating"
      @reload="emit('reload')"
      @new-object="emit('newObject')"
    >
      <p v-if="mappedError && mappedError !== error?.message">
        {{ mappedError }}
      </p>
    </EditorRecovery>
    <MailDraftFields
      :model-value="modelValue"
      :draft="draft"
      :entries="entries"
      :disabled="locked"
      :recipient-summary="recipientSummary"
      :errors="{
        to: $form.to?.error?.message,
        cc: $form.cc?.error?.message,
        bcc: $form.bcc?.error?.message,
        subject: $form.subject?.error?.message,
        body_text: $form.body_text?.error?.message,
      }"
    />
    <!-- 与多选控件平级注册，避免其数组或内部输入覆盖原有字符串 FormField。 -->
    <FormField
      v-for="name in recipientNames"
      :key="name"
      :name="name"
    />
    <Message
      v-if="dirty"
      severity="secondary"
      class="text-color"
      role="status"
      aria-live="polite"
    >
      有未保存的输入，请先保存后审阅。
    </Message>
    <div class="flex flex-wrap gap-3">
      <Button
        type="submit"
        name="save-draft"
        label="保存草稿"
        :disabled="locked || !dirty"
        :loading="busy && activeOperation === 'save'"
      />
      <Button
        type="submit"
        name="submit-draft"
        label="提交审批"
        severity="secondary"
        :disabled="!canSubmit"
        :loading="busy && activeOperation === 'submit'"
      />
    </div>
    <p v-if="!draft.to.length">
      提交审批前至少填写一位收件人。
    </p>
    <details class="space-y-3 rounded-lg border border-surface-200 p-4">
      <summary class="cursor-pointer font-medium">
        使用模型草拟正文
      </summary>
      <p>请先保存当前版本。模型只提供待审阅正文。</p>
      <div class="grid gap-1">
        <label for="mail-instruction">草拟要求</label>
        <Textarea
          id="mail-instruction"
          v-model="instruction"
          :form-control="{ novalidate: true }"
          :disabled="locked"
          maxlength="2000"
          rows="3"
          fluid
        />
      </div>
      <!-- 生成不提交整张 Form：已保存的空白草稿仍能显式生成正文，意图和安全守卫归原 hook。 -->
      <Button
        type="button"
        name="generate-draft"
        label="草拟正文"
        :disabled="locked || dirty || !instruction.trim()"
        :loading="generationLoading"
        :aria-busy="generationLoading"
        @click="generateBody"
      />
    </details>
    <Message
      v-if="generationRunning"
      role="status"
      aria-live="polite"
      severity="secondary"
    >
      正在草拟，请等待服务端保存结果。{{
        generationConnection === 'connected'
          ? ''
          : '连接恢复中，任务状态以服务端为准。'
      }}
    </Message>
    <ProblemMessage
      v-if="generationFailed"
      description="草拟失败，原草稿仍保留。请检查任务历史并重新加载后再试。"
    />
    <p
      v-if="draft.status !== 'editing'"
      class="rounded-lg border-l-4 border-orange-600 bg-orange-50 p-3"
    >
      当前版本不可编辑。待审批时，请先撤回审批任务再修改。
    </p>
  </Form>
</template>

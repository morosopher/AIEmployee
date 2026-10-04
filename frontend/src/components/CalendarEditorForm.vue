<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Form, {
  type FormInstance,
  type FormSubmitEvent,
} from '@primevue/forms/form'
import FormField from '@primevue/forms/formfield'
import Button from 'primevue/button'
import Message from 'primevue/message'
import type { CalendarProposal } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import type { CalendarEditorForm } from '@/features/calendar/useCalendarProposalEditor'
import type { ActionRecovery } from '@/features/actions/recovery'
import { calendarResolver } from '@/features/calendar/schema'
import { problemToFormError } from '@/features/forms/problemFields'
import CalendarProposalFields from './CalendarProposalFields.vue'
import CalendarTargetFields from './CalendarTargetFields.vue'
import EditorRecovery from './EditorRecovery.vue'

/**
 * Form/zod/DatePicker 的异步边界；只持有展示校验状态，不复制业务输入或请求。
 * 原 dirty/sourceDirty 两个 status 逐字保留，恢复说明与真实 error_code 的映射共用原唯一 alert。
 * 只有保存触发整表校验；选来源与逐项确认继续独立发出用户意图，绝不自动确认或提交审批。
 */
const props = defineProps<{
  modelValue: CalendarEditorForm
  proposal: CalendarProposal
  entries: ConnectionCatalogEntry[]
  locked: boolean
  busy: boolean
  dirty: boolean
  fieldsDirty: boolean
  sourceDirty: boolean
  error: ActionRecovery | null
  recoveryBusy: boolean
  newVersionAvailable: boolean
}>()
const emit = defineEmits<{
  save: []
  confirmCalendar: []
  reload: []
  newObject: []
  newVersion: []
}>()
const formApi = ref<FormInstance | null>(null)
const initialValues = ref({ ...props.modelValue })
const formKey = ref(0)
const saving = ref(false)
const mappedError = computed(() =>
  props.error?.problem ? problemToFormError(props.error.problem) : null,
)
const fieldNames = [
  'title',
  'starts_at',
  'ends_at',
  'timezone',
  'all_day',
  'attendees',
] as const
/** 仅真正采纳新 proposal 引用才重建；409 不采纳，因此输入及格式状态原样保留。 */
watch(
  () => props.proposal,
  () => {
    initialValues.value = { ...props.modelValue }
    formKey.value += 1
  },
)
/** 控件直接写回原 form；平级注册只同步校验值，防止 DatePicker 的 Date 覆盖业务墙上字符串。 */
for (const field of fieldNames)
  watch(
    () => props.modelValue[field],
    (value) => formApi.value?.setFieldValue(field, value),
    { flush: 'sync' },
  )
watch(
  () => props.busy,
  (busy) => {
    if (!busy) saving.value = false
  },
)
/** @param event 真实 resolver 结果；原 locked/fieldsDirty/sourceDirty 守卫仍为保存前置条件。 */
function saveForm(event: FormSubmitEvent): void {
  if (!event.valid || props.locked || !props.fieldsDirty || props.sourceDirty)
    return
  saving.value = true
  emit('save')
}
</script>
<template>
  <Form
    :key="formKey"
    ref="formApi"
    v-slot="$form"
    :initial-values="initialValues"
    :resolver="calendarResolver"
    :validate-on-value-update="false"
    :validate-on-blur="false"
    aria-label="日程提案表单"
    class="space-y-4"
    @submit="saveForm"
  >
    <EditorRecovery
      :error="error"
      :busy="recoveryBusy"
      :new-version-available="newVersionAvailable"
      @reload="emit('reload')"
      @new-object="emit('newObject')"
      @new-version="emit('newVersion')"
    >
      <p v-if="mappedError && mappedError !== error?.message">
        {{ mappedError }}
      </p>
    </EditorRecovery>
    <CalendarTargetFields
      :model-value="modelValue"
      :proposal="proposal"
      :entries="entries"
      :disabled="locked"
      :fields-dirty="fieldsDirty"
      @confirm="emit('confirmCalendar')"
    />
    <CalendarProposalFields
      :model-value="modelValue"
      :disabled="locked"
      :errors="{
        title: $form.title?.error?.message,
        timezone: $form.timezone?.error?.message,
        starts_at: $form.starts_at?.error?.message,
        ends_at: $form.ends_at?.error?.message,
        attendees: $form.attendees?.error?.message,
      }"
    />
    <FormField
      v-for="field in fieldNames"
      :key="field"
      :name="field"
    />
    <Message
      v-if="dirty"
      role="status"
      aria-live="polite"
      severity="secondary"
    >
      有未保存或未确认的修改，请保存后重新检查。
    </Message>
    <Message
      v-if="sourceDirty"
      role="status"
      aria-live="polite"
      severity="secondary"
    >
      请先确认目标日历，再编辑其他字段。
    </Message>
    <Button
      type="submit"
      name="save-proposal"
      label="保存提案"
      :disabled="locked || !fieldsDirty || sourceDirty"
      :loading="busy && saving"
    />
  </Form>
</template>

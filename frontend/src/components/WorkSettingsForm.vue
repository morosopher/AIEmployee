<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import Form, {
  type FormInstance,
  type FormSubmitEvent,
} from '@primevue/forms/form'
import FormField from '@primevue/forms/formfield'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Select from 'primevue/select'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { useWorkSettings } from '@/features/settings/useWorkSettings'
import { providerLabel } from '@/features/actions/presentation'
import {
  normalizeBriefTime,
  settingsResolver,
  settingsSchema,
  timezoneOptions,
} from '@/features/settings/schema'
import { problemToFormError } from '@/features/forms/problemFields'
import type { UserSettings, WorkingHours } from '@/api/types'
import WorkingHoursFields from './WorkingHoursFields.vue'

/** 只通过原 hook 读取／保存；Form 保存格式状态，不接管请求或领域判定。 */
const emit = defineEmits<{ 'dirty-change': [dirty: boolean] }>()
const {
  form,
  loading,
  saving,
  saved,
  error,
  validation,
  catalog,
  mailAccounts,
  calendarAccounts,
  calendars,
  inactiveMail,
  inactiveCalendar,
  load,
  save,
  chooseCalendarAccount,
} = useWorkSettings()
const formApi = ref<FormInstance | null>(null)
const formKey = ref(0)
const initialValues = ref<UserSettings | null>(null)
const timezones = computed(() => timezoneOptions(form.value?.timezone ?? 'UTC'))
const mappedError = computed(() =>
  error.value?.problem ? problemToFormError(error.value.problem) : null,
)
const retentionFields = [
  { name: 'email_body_retention_days', label: '邮件正文保留天数' },
  { name: 'source_metadata_retention_days', label: '来源元数据保留天数' },
  { name: 'workspace_history_retention_days', label: '工作区历史保留天数' },
] as const
/** 成功保存／显式重载会替换快照引用；同步重建 Form，清除旧字段状态而不影响 409 草稿。 */
watch(
  form,
  (snapshot) => {
    initialValues.value = snapshot
      ? { ...snapshot, brief_time: normalizeBriefTime(snapshot.brief_time) }
      : null
    formKey.value += 1
  },
  { immediate: true },
)
const dirty = computed(() =>
  Object.keys(settingsSchema.shape).some(
    (name) => formApi.value?.getFieldState(name)?.dirty === true,
  ),
)
watch(dirty, (value) => emit('dirty-change', value), { immediate: true })

/** @returns 可选目标包含失效原值；仅提示重选，绝不自动替换为其他连接。 */
const mailOptions = computed(() => [
  { label: '未设置', value: null },
  ...(inactiveMail.value
    ? [
        {
          label: '默认账户已不可用，请重选',
          value: form.value?.default_mail_connection_id,
          disabled: true,
        },
      ]
    : []),
  ...mailAccounts.value.map((entry) => ({
    label: `${providerLabel(entry.connection.provider)} · ${entry.connection.account_email}`,
    value: entry.connection.id,
  })),
])
const calendarAccountOptions = computed(() => [
  { label: '未设置', value: null },
  ...(inactiveCalendar.value
    ? [
        {
          label: '默认日历账户已不可用，请重选',
          value: form.value?.default_calendar_connection_id,
          disabled: true,
        },
      ]
    : []),
  ...calendarAccounts.value.map((entry) => ({
    label: `${providerLabel(entry.connection.provider)} · ${entry.connection.account_email}`,
    value: entry.connection.id,
  })),
])
const calendarOptions = computed(() => [
  { label: '请选择日历', value: null },
  ...(form.value?.default_calendar_id &&
  !calendars.value.some(
    (calendar) => calendar.id === form.value?.default_calendar_id,
  )
    ? [
        {
          label: '默认日历已不可用，请重选',
          value: form.value.default_calendar_id,
          disabled: true,
        },
      ]
    : []),
  ...calendars.value.map((calendar) => ({
    label: `${calendar.name}（${calendar.timezone}）`,
    value: calendar.id,
  })),
])
/** @param value 用户显式日历账户选择。重复选当前账户不清日历；变更沿用 hook 的清空规则。 */
function selectCalendarAccount(value: unknown): void {
  if (
    !form.value ||
    (typeof value !== 'string' && value !== null) ||
    value === form.value.default_calendar_connection_id
  )
    return
  chooseCalendarAccount(value ?? '')
  formApi.value?.setFieldValue('default_calendar_id', null)
}
/** @param hours 用户的完整七日草稿；同时同步 hook 投影和单一 Form 字段，避免动态索引残留。 */
function changeHours(hours: WorkingHours): void {
  if (form.value) form.value.working_hours = hours
  formApi.value?.setFieldValue('working_hours', hours)
}
/** @param event 真实 zodResolver 结果；仅有效的完整格式值同步原草稿，随后调用唯一 save。 */
async function submit(event: FormSubmitEvent): Promise<void> {
  if (!event.valid || !form.value || loading.value || saving.value) return
  const result = settingsSchema.safeParse(event.values)
  if (!result.success) return
  Object.assign(form.value, result.data)
  await save()
}
</script>
<template>
  <section
    aria-label="工作设置"
    class="space-y-4"
  >
    <div
      v-if="loading"
      class="space-y-2"
    >
      <Message
        severity="secondary"
        role="status"
        aria-live="polite"
      >
        正在加载设置…
      </Message>
      <Skeleton height="3rem" />
    </div>
    <!-- 仅提交时显示／清除格式错误，避免 blur 重排把鼠标按下后的保存或添加按钮移走。 -->
    <Form
      v-if="initialValues && form"
      :key="formKey"
      ref="formApi"
      v-slot="$form"
      :initial-values="initialValues"
      :resolver="settingsResolver"
      :validate-on-value-update="false"
      :validate-on-blur="false"
      aria-labelledby="work-settings-heading"
      class="space-y-4"
      @submit="submit"
    >
      <fieldset
        :disabled="loading || saving"
        class="min-w-0 space-y-4"
      >
        <legend
          id="work-settings-heading"
          class="text-lg font-semibold"
        >
          工作偏好与保留周期
        </legend>
        <div class="grid gap-4 md:grid-cols-2">
          <div class="grid gap-1">
            <label for="settings-timezone">IANA 时区</label>
            <Select
              input-id="settings-timezone"
              name="timezone"
              :options="timezones"
              filter
              editable
              fluid
              :disabled="loading || saving"
              :invalid="$form.timezone?.invalid"
              :pt="{
                pcInputText: {
                  root: {
                    'aria-describedby': 'timezone-error',
                    'aria-invalid': $form.timezone?.invalid || undefined,
                  },
                },
              }"
            />
            <Message
              v-if="$form.timezone?.invalid"
              id="timezone-error"
              severity="error"
              size="small"
              variant="simple"
            >
              {{ $form.timezone.error.message }}
            </Message>
          </div>
          <div class="grid gap-1">
            <label for="settings-locale">语言</label>
            <InputText
              id="settings-locale"
              name="locale"
              :disabled="loading || saving"
              :invalid="$form.locale?.invalid"
              :aria-invalid="$form.locale?.invalid || undefined"
              aria-describedby="locale-error"
            />
            <Message
              v-if="$form.locale?.invalid"
              id="locale-error"
              severity="error"
              size="small"
              variant="simple"
            >
              {{ $form.locale.error.message }}
            </Message>
          </div>
          <div class="grid gap-1">
            <label for="settings-brief-time">简报时间</label>
            <InputText
              id="settings-brief-time"
              name="brief_time"
              placeholder="HH:mm"
              :disabled="loading || saving"
              :invalid="$form.brief_time?.invalid"
              :aria-invalid="$form.brief_time?.invalid || undefined"
              aria-describedby="brief-time-error"
            />
            <Message
              v-if="$form.brief_time?.invalid"
              id="brief-time-error"
              severity="error"
              size="small"
              variant="simple"
            >
              {{ $form.brief_time.error.message }}
            </Message>
          </div>
          <div
            v-for="field in retentionFields"
            :key="field.name"
            class="grid gap-1"
          >
            <label :for="`settings-${field.name}`">{{ field.label }}</label>
            <InputNumber
              :input-id="`settings-${field.name}`"
              :name="field.name"
              :use-grouping="false"
              :disabled="loading || saving"
              :invalid="$form[field.name]?.invalid"
              :pt="{
                pcInputText: {
                  root: { 'aria-describedby': `${field.name}-error` },
                },
              }"
              fluid
            />
            <Message
              v-if="$form[field.name]?.invalid"
              :id="`${field.name}-error`"
              severity="error"
              size="small"
              variant="simple"
            >
              {{ $form[field.name]?.error?.message }}
            </Message>
          </div>
          <div class="grid gap-1">
            <label
              id="settings-mail-label"
              for="settings-mail"
            >默认发送账户</label>
            <Select
              input-id="settings-mail"
              aria-labelledby="settings-mail-label"
              name="default_mail_connection_id"
              :options="mailOptions"
              option-label="label"
              option-value="value"
              option-disabled="disabled"
              placeholder="未设置"
              :disabled="loading || saving"
              fluid
              @change="form.default_mail_connection_id = $event.value"
            >
              <template #value="selected">
                {{
                  mailOptions.find((option) => option.value === selected.value)
                    ?.label ?? '未设置'
                }}
              </template>
            </Select>
          </div>
          <div class="grid gap-1">
            <label
              id="settings-calendar-account-label"
              for="settings-calendar-account"
            >默认日历账户</label>
            <Select
              input-id="settings-calendar-account"
              aria-labelledby="settings-calendar-account-label"
              name="default_calendar_connection_id"
              :options="calendarAccountOptions"
              option-label="label"
              option-value="value"
              option-disabled="disabled"
              placeholder="未设置"
              :disabled="loading || saving"
              fluid
              @change="selectCalendarAccount($event.value)"
            >
              <template #value="selected">
                {{
                  calendarAccountOptions.find(
                    (option) => option.value === selected.value,
                  )?.label ?? '未设置'
                }}
              </template>
            </Select>
          </div>
          <div class="grid gap-1">
            <label
              id="settings-calendar-label"
              for="settings-calendar"
            >默认日历</label>
            <Select
              input-id="settings-calendar"
              aria-labelledby="settings-calendar-label"
              name="default_calendar_id"
              :options="calendarOptions"
              option-label="label"
              option-value="value"
              option-disabled="disabled"
              placeholder="请选择日历"
              :disabled="loading || saving"
              fluid
              @change="form.default_calendar_id = $event.value"
            >
              <template #value="selected">
                {{
                  calendarOptions.find(
                    (option) => option.value === selected.value,
                  )?.label ?? '请选择日历'
                }}
              </template>
            </Select>
          </div>
          <div class="grid gap-1">
            <label for="settings-buffer">会议缓冲（0–120 分钟）</label>
            <InputNumber
              input-id="settings-buffer"
              name="meeting_buffer_minutes"
              :use-grouping="false"
              :disabled="loading || saving"
              :invalid="$form.meeting_buffer_minutes?.invalid"
              :pt="{
                pcInputText: {
                  root: { 'aria-describedby': 'meeting-buffer-error' },
                },
              }"
              fluid
            />
            <Message
              v-if="$form.meeting_buffer_minutes?.invalid"
              id="meeting-buffer-error"
              severity="error"
              size="small"
              variant="simple"
            >
              {{ $form.meeting_buffer_minutes.error.message }}
            </Message>
          </div>
        </div>
        <Message
          v-if="catalog.loading.value"
          severity="secondary"
          role="status"
          aria-live="polite"
        >
          正在读取可用账户与日历…
        </Message>
        <Message
          v-if="
            catalog.error.value ||
              catalog.entries.value.some((entry) => entry.error)
          "
          severity="error"
        >
          部分账户目录加载失败，已保留原默认选择。
          <Button
            type="button"
            label="重试目录"
            severity="secondary"
            :disabled="loading || saving"
            @click="catalog.load"
          />
        </Message>
        <!-- FormField 与时间控件平级：内部 InputText 不继承整周字段名，避免字符串覆盖七日对象。 -->
        <WorkingHoursFields
          :model-value="form.working_hours"
          :disabled="loading || saving"
          :invalid="$form.working_hours?.invalid"
          :describedby="
            $form.working_hours?.invalid ? 'working-hours-error' : undefined
          "
          @update:model-value="changeHours"
        />
        <FormField
          v-slot="$field"
          name="working_hours"
        >
          <Message
            v-if="$field.invalid"
            id="working-hours-error"
            severity="error"
            size="small"
            variant="simple"
          >
            <p
              v-for="(issue, index) in $field.errors"
              :key="index"
            >
              {{ issue.message }}
            </p>
          </Message>
        </FormField>
        <Button
          type="submit"
          :label="saving ? '正在保存…' : '保存'"
          :loading="saving"
          :disabled="loading || saving"
        />
      </fieldset>
    </Form>
    <Message
      v-if="saved"
      severity="success"
      role="status"
      aria-live="polite"
    >
      已保存
    </Message>
    <Message
      v-if="validation"
      severity="error"
    >
      {{ validation }}
    </Message>
    <Message
      v-if="error"
      severity="error"
    >
      <p v-if="mappedError && mappedError !== error.message">
        {{ mappedError }}
      </p>
      <p>
        {{ error.message }}
        <span v-if="error.traceId">追踪编号：{{ error.traceId }}</span>
      </p>
      <RouterLink
        v-if="error.action === 'reauthorize'"
        to="/connections"
        class="text-primary underline"
      >
        检查连接并重新授权
      </RouterLink>
      <Button
        type="button"
        label="重新加载设置"
        severity="secondary"
        :disabled="loading || saving"
        @click="load"
      />
    </Message>
  </section>
</template>

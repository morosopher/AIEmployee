<script setup lang="ts">
import { computed } from 'vue'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Select from 'primevue/select'
import Checkbox from 'primevue/checkbox'
import Chip from 'primevue/chip'
import Message from 'primevue/message'
import type { CalendarEditorForm } from '@/features/calendar/useCalendarProposalEditor'
import { calendarTimezoneOptions } from '@/features/calendar/schema'
import CalendarDateTimeInput from './CalendarDateTimeInput.vue'

/** 输入始终写回唯一 hook 表单；错误只展示 Form/zod 格式结果，参会人 Chip 不改变原字符串或数量规则。 */
defineProps<{
  disabled: boolean
  errors?: Partial<
    Record<'title' | 'timezone' | 'starts_at' | 'ends_at' | 'attendees', string>
  >
}>()
const form = defineModel<CalendarEditorForm>({ required: true })
const timezones = computed(() => calendarTimezoneOptions(form.value.timezone))
const attendees = computed(() =>
  form.value.attendees
    .split(/[;,\n]/)
    .map((value) => value.trim())
    .filter(Boolean),
)
const notificationOptions = [
  { label: '请选择', value: '' },
  { label: '通知所有参会人', value: 'all' },
  { label: '不发送通知', value: 'none' },
]
/**
 * @param allDay 明确的类型选择。转全天取原年月日；转定时保留日期并要求用户明确填写时间，不推断默认小时。
 * 两个日期字符串直接同步原 form，触发原 hook 的同步冲突失效，不维护第二份时间草稿。
 */
function changeAllDay(allDay: boolean): void {
  form.value.all_day = allDay
  if (allDay)
    for (const key of ['starts_at', 'ends_at'] as const)
      if (/^\d{4}-\d{2}-\d{2}T/.test(form.value[key]))
        form.value[key] = form.value[key].slice(0, 10)
}
</script>
<template>
  <div class="grid min-w-0 gap-4 md:grid-cols-2">
    <div class="grid gap-1 md:col-span-2">
      <label for="calendar-title">日程标题</label>
      <InputText
        id="calendar-title"
        v-model="form.title"
        aria-label="日程标题"
        maxlength="255"
        :disabled="disabled"
        :invalid="!!errors?.title"
        :aria-invalid="!!errors?.title || undefined"
        aria-describedby="calendar-title-error"
      />
      <Message
        v-if="errors?.title"
        id="calendar-title-error"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors.title }}
      </Message>
    </div>
    <div class="grid gap-1">
      <label for="calendar-timezone">IANA 时区</label>
      <Select
        v-model="form.timezone"
        input-id="calendar-timezone"
        :options="timezones"
        editable
        filter
        fluid
        :disabled="disabled"
        :invalid="!!errors?.timezone"
        :pt="{
          pcInputText: {
            root: {
              'aria-label': 'IANA 时区',
              'aria-invalid': !!errors?.timezone || undefined,
              'aria-describedby': 'calendar-timezone-error',
            },
          },
        }"
      />
      <Message
        v-if="errors?.timezone"
        id="calendar-timezone-error"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors.timezone }}
      </Message>
    </div>
    <div class="flex items-center gap-2">
      <Checkbox
        input-id="calendar-all-day"
        :model-value="form.all_day"
        binary
        :disabled="disabled"
        :pt="{ input: { 'aria-label': '全天日程' } }"
        @update:model-value="changeAllDay"
      />
      <label for="calendar-all-day">全天日程</label>
    </div>
    <div
      v-for="field in ['starts_at', 'ends_at'] as const"
      :key="field"
      class="grid gap-1"
    >
      <label :for="`calendar-${field}`">{{
        field === 'starts_at'
          ? form.all_day
            ? '开始日期'
            : '开始时间'
          : form.all_day
            ? '结束日期（不含）'
            : '结束时间'
      }}</label>
      <CalendarDateTimeInput
        v-model="form[field]"
        :input-id="`calendar-${field}`"
        :label="
          field === 'starts_at'
            ? form.all_day
              ? '开始日期'
              : '开始时间'
            : form.all_day
              ? '结束日期（不含）'
              : '结束时间'
        "
        :all-day="form.all_day"
        :disabled="disabled"
        :invalid="!!errors?.[field]"
        :describedby="`calendar-${field}-error`"
      />
      <Message
        v-if="errors?.[field]"
        :id="`calendar-${field}-error`"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors[field] }}
      </Message>
    </div>
    <p class="md:col-span-2">
      时间输入采用
      {{
        form.timezone
      }}。更改时区后，请重新确认时间。全天结束日期不包含当天。切回定时日程后，请明确填写开始和结束时间。
    </p>
    <div class="grid gap-1">
      <label for="calendar-attendees">参会人</label>
      <InputText
        id="calendar-attendees"
        v-model="form.attendees"
        aria-label="参会人"
        :disabled="disabled"
        autocomplete="off"
        :invalid="!!errors?.attendees"
        :aria-invalid="!!errors?.attendees || undefined"
        aria-describedby="calendar-attendees-error"
      />
      <ul
        v-if="attendees.length"
        aria-label="参会人列表"
        class="flex flex-wrap gap-2"
      >
        <li
          v-for="(attendee, index) in attendees"
          :key="index"
          class="min-w-0"
        >
          <Chip
            :label="attendee"
            class="max-w-full break-all"
          />
        </li>
      </ul>
      <Message
        v-if="errors?.attendees"
        id="calendar-attendees-error"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors.attendees }}
      </Message>
    </div>
    <div class="grid gap-1">
      <label
        id="calendar-notification-label"
        for="calendar-notification"
      >通知策略</label>
      <Select
        v-model="form.notification_policy"
        input-id="calendar-notification"
        aria-labelledby="calendar-notification-label"
        :options="notificationOptions"
        option-label="label"
        option-value="value"
        :disabled="disabled"
        fluid
      />
    </div>
    <div class="grid gap-1 md:col-span-2">
      <label for="calendar-location">地点</label><InputText
        id="calendar-location"
        v-model="form.location"
        aria-label="地点"
        :disabled="disabled"
        maxlength="16384"
      />
    </div>
    <div class="grid gap-1 md:col-span-2">
      <label for="calendar-description">描述</label><Textarea
        id="calendar-description"
        v-model="form.description"
        aria-label="描述"
        :disabled="disabled"
        maxlength="100000"
        rows="4"
        fluid
      />
    </div>
  </div>
</template>

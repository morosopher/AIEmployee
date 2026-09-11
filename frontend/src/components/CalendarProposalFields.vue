<script setup lang="ts">
import type { CalendarEditorForm } from '@/features/calendar/useCalendarProposalEditor'

/** 所有输入为纯文本与原生日期控件；时区由用户明确指定，全天结束日期不包含当天。 */
defineProps<{ disabled: boolean }>()
const form = defineModel<CalendarEditorForm>({ required: true })
</script>
<template>
  <div class="editor-fields">
    <label class="wide-field">日程标题<input
      v-model="form.title"
      aria-label="日程标题"
      maxlength="255"
      :disabled="disabled"
    ></label>
    <label>IANA 时区<input
      v-model="form.timezone"
      aria-label="IANA 时区"
      :disabled="disabled"
      maxlength="64"
    ></label>
    <label>全天日程<input
      v-model="form.all_day"
      aria-label="全天日程"
      type="checkbox"
      :disabled="disabled"
    ></label>
    <template v-if="form.all_day">
      <label>开始日期<input
        v-model="form.starts_at"
        aria-label="开始日期"
        type="date"
        :disabled="disabled"
      ></label><label>结束日期（不含）<input
        v-model="form.ends_at"
        aria-label="结束日期（不含）"
        type="date"
        :disabled="disabled"
      ></label>
    </template>
    <template v-else>
      <label>开始时间<input
        v-model="form.starts_at"
        aria-label="开始时间"
        type="datetime-local"
        step="1"
        :disabled="disabled"
      ></label><label>结束时间<input
        v-model="form.ends_at"
        aria-label="结束时间"
        type="datetime-local"
        step="1"
        :disabled="disabled"
      ></label>
    </template>
    <p class="wide-field">
      时间输入采用
      {{ form.timezone }}。更改时区后，请重新确认时间。全天结束日期不包含当天。
    </p>
    <label>参会人<input
      v-model="form.attendees"
      aria-label="参会人"
      :disabled="disabled"
      autocomplete="off"
    ></label>
    <label>通知策略<select
      v-model="form.notification_policy"
      aria-label="通知策略"
      :disabled="disabled"
    >
      <option value="">请选择</option>
      <option value="all">通知所有参会人</option>
      <option value="none">不发送通知</option>
    </select></label>
    <label class="wide-field">地点<input
      v-model="form.location"
      aria-label="地点"
      :disabled="disabled"
      maxlength="16384"
    ></label>
    <label class="wide-field">描述<textarea
      v-model="form.description"
      aria-label="描述"
      :disabled="disabled"
      maxlength="100000"
    />
    </label>
  </div>
</template>

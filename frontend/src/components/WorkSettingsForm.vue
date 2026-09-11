<script setup lang="ts">
import { useWorkSettings } from '@/features/settings/useWorkSettings'
import { providerLabel } from '@/features/actions/presentation'
import WorkingHoursFields from './WorkingHoursFields.vue'

/** 设置表单只编辑服务端完整快照，失效默认目标保持原值并提示明确重选。 */
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
const timezones = [
  'UTC',
  ...(typeof Intl.supportedValuesOf === 'function'
    ? Intl.supportedValuesOf('timeZone')
    : ['Asia/Shanghai', 'America/Los_Angeles']),
]
</script>
<template>
  <section aria-label="工作设置">
    <p
      v-if="loading"
      role="status"
    >
      正在加载设置…
    </p>
    <form
      v-if="form"
      @submit.prevent="save"
    >
      <fieldset :disabled="loading || saving">
        <legend>工作偏好与保留周期</legend>
        <label>IANA 时区
          <input
            v-model="form.timezone"
            name="timezone"
            list="iana-timezones"
            required
          ></label>
        <datalist id="iana-timezones">
          <option
            v-for="timezone in timezones"
            :key="timezone"
            :value="timezone"
          />
        </datalist>
        <label>语言 <input
          v-model="form.locale"
          required
        ></label>
        <label>简报时间 <input
          v-model="form.brief_time"
          type="time"
          required
        ></label>
        <label>邮件正文保留天数
          <input
            v-model.number="form.email_body_retention_days"
            type="number"
            min="1"
            required
          ></label>
        <label>来源元数据保留天数
          <input
            v-model.number="form.source_metadata_retention_days"
            type="number"
            min="1"
            required
          ></label>
        <label>工作区历史保留天数
          <input
            v-model.number="form.workspace_history_retention_days"
            type="number"
            min="1"
            required
          ></label>
        <label>默认发送账户<select
          v-model="form.default_mail_connection_id"
          name="default-mail"
        >
          <option :value="null">未设置</option>
          <option
            v-if="inactiveMail"
            :value="form.default_mail_connection_id"
            disabled
          >
            默认账户已不可用，请重选
          </option>
          <option
            v-for="entry in mailAccounts"
            :key="entry.connection.id"
            :value="entry.connection.id"
          >
            {{ providerLabel(entry.connection.provider) }} ·
            {{ entry.connection.account_email }}
          </option>
        </select></label>
        <label>默认日历账户<select
          name="default-calendar-connection"
          :value="form.default_calendar_connection_id || ''"
          @change="
            chooseCalendarAccount(($event.target as HTMLSelectElement).value)
          "
        >
          <option value="">未设置</option>
          <option
            v-if="inactiveCalendar"
            :value="form.default_calendar_connection_id || ''"
            disabled
          >
            默认日历账户已不可用，请重选
          </option>
          <option
            v-for="entry in calendarAccounts"
            :key="entry.connection.id"
            :value="entry.connection.id"
          >
            {{ providerLabel(entry.connection.provider) }} ·
            {{ entry.connection.account_email }}
          </option>
        </select></label>
        <label>默认日历<select
          v-model="form.default_calendar_id"
          name="default-calendar"
        >
          <option :value="null">请选择日历</option>
          <option
            v-if="
              form.default_calendar_id &&
                !calendars.some(
                  (calendar) => calendar.id === form?.default_calendar_id,
                )
            "
            :value="form.default_calendar_id"
            disabled
          >
            默认日历已不可用，请重选
          </option>
          <option
            v-for="calendar in calendars"
            :key="calendar.id"
            :value="calendar.id"
          >
            {{ calendar.name }}（{{ calendar.timezone }}）
          </option>
        </select></label>
        <p
          v-if="catalog.loading.value"
          role="status"
        >
          正在读取可用账户与日历…
        </p>
        <p
          v-if="
            catalog.error.value ||
              catalog.entries.value.some((entry) => entry.error)
          "
          role="alert"
        >
          部分账户目录加载失败，已保留原默认选择。<button
            type="button"
            @click="catalog.load"
          >
            重试目录
          </button>
        </p>
        <WorkingHoursFields v-model="form.working_hours" />
        <label>会议缓冲（0–120 分钟）<input
          v-model.number="form.meeting_buffer_minutes"
          name="meeting-buffer"
          type="number"
          min="0"
          max="120"
          step="1"
          required
        ></label>
        <button type="submit">
          {{ saving ? '正在保存…' : '保存' }}
        </button>
      </fieldset>
    </form>
    <p
      v-if="saved"
      role="status"
    >
      已保存
    </p>
    <p
      v-if="validation"
      role="alert"
    >
      {{ validation }}
    </p>
    <div
      v-if="error"
      role="alert"
    >
      <p>
        {{ error.message }}
        <span v-if="error.traceId">追踪编号：{{ error.traceId }}</span>
      </p>
      <button
        type="button"
        @click="load"
      >
        重新加载设置
      </button>
    </div>
  </section>
</template>
<style scoped>
form > fieldset {
  border: 0;
  padding: 0;
  display: grid;
  gap: 1rem;
}
label {
  display: grid;
  gap: 0.35rem;
  max-width: 32rem;
}
input,
select {
  padding: 0.5rem;
  min-width: 0;
}
[role='alert'] {
  color: #a61b1b;
}
button:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

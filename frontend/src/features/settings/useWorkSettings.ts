import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getSettings, updateSettings } from '@/api/settings'
import type { UserSettings } from '@/api/types'
import {
  hasCapability,
  useConnectionCatalog,
} from '@/composables/useConnectionCatalog'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'

/**
 * 工作设置只消费完整 API 快照；默认失效时保持原值，不自动切换账户或主日历。
 * @returns 编辑草表、可选目标和明确加载/保存/恢复状态。
 */
export function useWorkSettings() {
  const form = ref<UserSettings | null>(null),
    loading = ref(true),
    saving = ref(false),
    saved = ref(false)
  const error = ref<ActionRecovery | null>(null),
    validation = ref<string | null>(null)
  const catalog = useConnectionCatalog()
  let disposed = false
  let generation = 0
  onUnmounted(() => {
    disposed = true
    generation += 1
  })
  const mailAccounts = computed(() =>
    catalog.entries.value.filter((entry) => hasCapability(entry, 'mail.send')),
  )
  const calendarAccounts = computed(() =>
    catalog.entries.value.filter((entry) =>
      hasCapability(entry, 'calendar.write'),
    ),
  )
  const selectedCalendarAccount = computed(() =>
    calendarAccounts.value.find(
      (entry) =>
        entry.connection.id === form.value?.default_calendar_connection_id,
    ),
  )
  const calendars = computed(
    () =>
      selectedCalendarAccount.value?.capabilities?.provider_calendars.filter(
        (calendar) => calendar.can_write,
      ) ?? [],
  )
  const inactiveMail = computed(() =>
    Boolean(
      form.value?.default_mail_connection_id &&
      !mailAccounts.value.some(
        (entry) =>
          entry.connection.id === form.value?.default_mail_connection_id,
      ),
    ),
  )
  const inactiveCalendar = computed(() =>
    Boolean(
      form.value?.default_calendar_connection_id &&
      !calendarAccounts.value.some(
        (entry) =>
          entry.connection.id === form.value?.default_calendar_connection_id,
      ),
    ),
  )

  /** 从服务端加载设置；目录独立失败不会让现有默认目标变成空值。 */
  async function load(): Promise<void> {
    const owner = ++generation
    loading.value = true
    error.value = null
    try {
      const value = await getSettings()
      if (!disposed && owner === generation) form.value = value
    } catch (cause) {
      if (!disposed && owner === generation) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === generation) loading.value = false
    }
  }
  onMounted(() => {
    void load()
    void catalog.load()
  })

  /**
   * 只做输入语法校验，工作时间冲突和账户权限由后端再次判断。
   * @returns 保存后以服务端完整结果覆盖草表，不发送只读 updated_at。
   */
  async function save(): Promise<void> {
    const value = form.value
    if (!value || loading.value || saving.value) return
    validation.value = null
    saved.value = false
    error.value = null
    if (
      !Number.isInteger(value.meeting_buffer_minutes) ||
      value.meeting_buffer_minutes < 0 ||
      value.meeting_buffer_minutes > 120
    ) {
      validation.value = '会议缓冲必须是 0–120 分钟的整数。'
      return
    }
    try {
      new Intl.DateTimeFormat('zh-CN', { timeZone: value.timezone })
    } catch {
      validation.value = '请输入有效的 IANA 时区。'
      return
    }
    if (
      Object.values(value.working_hours).some((intervals) =>
        intervals.some(
          ([start, end]) =>
            !/^\d{2}:\d{2}$/.test(start) ||
            !/^\d{2}:\d{2}$/.test(end) ||
            start >= end,
        ),
      )
    ) {
      validation.value = '每个工作区间的结束时间必须晚于开始时间。'
      return
    }
    // 仅提交既定的可编辑字段；updated_at 是服务端读取事实，不进入 PATCH。
    const patch = {
      timezone: value.timezone,
      locale: value.locale,
      brief_time: value.brief_time,
      email_body_retention_days: value.email_body_retention_days,
      source_metadata_retention_days: value.source_metadata_retention_days,
      workspace_history_retention_days: value.workspace_history_retention_days,
      default_mail_connection_id: value.default_mail_connection_id,
      default_calendar_connection_id: value.default_calendar_connection_id,
      default_calendar_id: value.default_calendar_id,
      working_hours: value.working_hours,
      meeting_buffer_minutes: value.meeting_buffer_minutes,
    }
    const owner = generation
    saving.value = true
    try {
      const response = await updateSettings(patch)
      if (!disposed && owner === generation) {
        form.value = response
        saved.value = true
      }
    } catch (cause) {
      if (!disposed && owner === generation) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === generation) saving.value = false
    }
  }
  /** @param id 用户明确选择的日历连接。清空旧日历，等待用户再次选择，禁止静默匹配。 */
  function chooseCalendarAccount(id: string): void {
    if (!form.value) return
    form.value.default_calendar_connection_id = id || null
    form.value.default_calendar_id = null
  }
  return {
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
  }
}

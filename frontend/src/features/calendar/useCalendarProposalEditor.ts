import { computed, onUnmounted, reactive, ref, watch, type Ref } from 'vue'
import {
  getCalendarProposal,
  submitCalendarProposal,
  suggestCalendarTimes,
  updateCalendarProposal,
  type CalendarConfirmationInput,
  type CalendarProposalChanges,
  type UpdateCalendarProposalInput,
} from '@/api/calendar'
import { createRequestIntent, type RequestIntent } from '@/api/requestIntent'
import type {
  CalendarAvailability,
  CalendarConfirmation,
  CalendarPreviewFields,
  CalendarProposal,
} from '@/api/types'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'
import {
  EditorInputError,
  recipientList,
  validateRecipientGroups,
} from '@/features/actions/editorInput'
import { instantToWall, wallToInstant } from './time'

/** 日期/墙上时间仅用于输入；API 始终接收明确日期或带 offset 的时刻。 */
export interface CalendarEditorForm {
  connection_id: string
  calendar_id: string
  title: string
  description: string
  location: string
  starts_at: string
  ends_at: string
  timezone: string
  all_day: boolean
  attendees: string
  notification_policy: '' | 'all' | 'none'
}

/**
 * @param proposalId 当前受保护编辑路由的本地提案 ID。
 * @param submitted 接受真实 task_id 后导航，不生成本地审批状态。
 * @returns 保存、单项确认和服务端候选；冲突算法完全留在服务端。
 */
export function useCalendarProposalEditor(
  proposalId: Ref<string>,
  submitted: (taskId: string) => Promise<unknown>,
) {
  const proposal = ref<CalendarProposal | null>(null)
  const form = reactive<CalendarEditorForm>({
    connection_id: '',
    calendar_id: '',
    title: '',
    description: '',
    location: '',
    starts_at: '',
    ends_at: '',
    timezone: 'UTC',
    all_day: false,
    attendees: '',
    notification_policy: '',
  })
  const baseline = ref<CalendarEditorForm | null>(null),
    loading = ref(false),
    pending = ref(false),
    refreshRequired = ref(false)
  const error = ref<ActionRecovery | null>(null),
    availability = ref<CalendarAvailability | null>(null)
  const evidenceKey = ref('')
  const currentKey = computed(() =>
    JSON.stringify([
      form.connection_id,
      form.calendar_id,
      form.starts_at,
      form.ends_at,
      form.timezone,
      form.all_day,
    ]),
  )
  const sourceDirty = computed(
    () =>
      form.connection_id !== proposal.value?.connection_id ||
      form.calendar_id !== proposal.value?.calendar_id,
  )
  const fieldsDirty = computed(
    () =>
      baseline.value !== null &&
      Object.keys(form).some(
        (key) =>
          key !== 'connection_id' &&
          key !== 'calendar_id' &&
          form[key as keyof CalendarEditorForm] !==
            baseline.value?.[key as keyof CalendarEditorForm],
      ),
  )
  const dirty = computed(() => sourceDirty.value || fieldsDirty.value)
  const busy = computed(() => loading.value || pending.value)
  const locked = computed(
    () =>
      busy.value ||
      refreshRequired.value ||
      proposal.value?.status !== 'editing',
  )
  const fresh = computed(() => currentKey.value === evidenceKey.value)
  const conflicts = computed(() =>
    fresh.value && proposal.value?.editor_facts?.conflict_status === 'checked'
      ? proposal.value.editor_facts.conflicts
      : null,
  )
  const candidates = computed(() => (fresh.value ? availability.value : null))
  let epoch = 0,
    disposed = false
  let submitIntent: { version: number; intent: RequestIntent } | null = null
  onUnmounted(() => {
    disposed = true
    epoch += 1
    proposal.value = null
    baseline.value = null
    form.description = ''
  })

  /** 完整服务端版本是唯一的确认与冲突来源；mutation 的 null facts 不能伪装成已检查。 */
  function adopt(value: CalendarProposal): void {
    proposal.value = value
    refreshRequired.value = false
    const zone = value.timezone ?? 'UTC'
    Object.assign(form, {
      connection_id: value.connection_id,
      calendar_id: value.calendar_id,
      title: value.title ?? '',
      description: value.description ?? '',
      location: value.location ?? '',
      starts_at: value.starts_at
        ? value.all_day
          ? value.starts_at
          : instantToWall(value.starts_at, zone)
        : '',
      ends_at: value.ends_at
        ? value.all_day
          ? value.ends_at
          : instantToWall(value.ends_at, zone)
        : '',
      timezone: zone,
      all_day: value.all_day ?? false,
      attendees: value.attendees.join(', '),
      notification_policy: value.notification_policy ?? '',
    })
    baseline.value = { ...form }
    availability.value = value.availability
    evidenceKey.value = currentKey.value
  }
  /** 显式刷新读取原始 before 和当前冲突；不能从当前供应商事件重建 before。 */
  async function reload(): Promise<void> {
    const owner = ++epoch,
      id = proposalId.value
    if (!id) {
      proposal.value = null
      loading.value = false
      return
    }
    loading.value = true
    error.value = null
    try {
      const value = await getCalendarProposal(id)
      if (!disposed && owner === epoch) adopt(value)
    } catch (cause) {
      if (!disposed && owner === epoch) failure(cause)
    } finally {
      if (!disposed && owner === epoch) loading.value = false
    }
  }
  watch(
    proposalId,
    () => {
      proposal.value = null
      baseline.value = null
      pending.value = false
      refreshRequired.value = false
      submitIntent = null
      void reload()
    },
    { immediate: true },
  )
  // 时间、时区、全天和来源一经编辑就使旧候选失效；即使改回原值，也须重新向服务端请求。
  watch(
    currentKey,
    () => {
      availability.value = null
      evidenceKey.value = ''
    },
    { flush: 'sync' },
  )

  /** 保留未修改的时刻原始精度；只有用户改变墙上时间或时区才执行显式转换。 */
  function temporal(key: 'starts_at' | 'ends_at'): string | null {
    const value = form[key]
    if (!value) return null
    if (
      baseline.value &&
      value === baseline.value[key] &&
      form.timezone === baseline.value.timezone &&
      form.all_day === baseline.value.all_day
    )
      return proposal.value?.[key] ?? null
    if (form.all_day) {
      if (
        !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
        !Number.isFinite(Date.parse(`${value}T00:00:00Z`)) ||
        new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value
      )
        throw new EditorInputError('请填写有效的全天日期。')
      return value
    }
    return wallToInstant(value, form.timezone)
  }
  /** 局部空 shell 可保存，但必须保持日期/时刻的表示和顺序一致。 */
  function input(): CalendarProposalChanges {
    try {
      new Intl.DateTimeFormat('en', { timeZone: form.timezone })
    } catch {
      throw new EditorInputError('请填写有效的 IANA 时区。')
    }
    const starts_at = temporal('starts_at'),
      ends_at = temporal('ends_at')
    if (
      starts_at &&
      ends_at &&
      (form.all_day
        ? starts_at >= ends_at
        : Date.parse(starts_at) >= Date.parse(ends_at))
    )
      throw new EditorInputError(
        '结束时间必须晚于开始时间；全天结束日期不包含当天。',
      )
    const attendees = recipientList(form.attendees)
    validateRecipientGroups(attendees)
    return {
      title: form.title.trim() || null,
      description: form.description || null,
      location: form.location || null,
      starts_at,
      ends_at,
      timezone: form.timezone,
      all_day: form.all_day,
      attendees,
      notification_policy: form.notification_policy || null,
    }
  }
  const after = computed<CalendarPreviewFields | null>(() => {
    const value = proposal.value
    return value?.title &&
      value.starts_at &&
      value.ends_at &&
      value.timezone &&
      value.all_day !== null
      ? {
          title: value.title,
          description: value.description,
          location: value.location,
          starts_at: value.starts_at,
          ends_at: value.ends_at,
          timezone: value.timezone,
          all_day: value.all_day,
          attendees: value.attendees,
        }
      : null
  })
  const canSubmit = computed(
    () =>
      !locked.value &&
      !dirty.value &&
      proposal.value?.required_confirmations.length === 0 &&
      after.value !== null &&
      proposal.value.notification_policy !== null &&
      (proposal.value.operation_kind === 'create' ||
        proposal.value.changed_fields.length > 0) &&
      proposal.value.editor_facts?.before_status !== 'unavailable',
  )

  /** 安全错误状态不包含供应商响应或用户描述。 */
  function failure(cause: unknown): void {
    error.value =
      cause instanceof EditorInputError
        ? { message: cause.message, traceId: null, action: 'retry' }
        : actionRecovery(cause)
  }
  /** 保存或确认后先采纳新版本，再 GET 重读 facts；GET 失败也不能回退 CAS 版本。 */
  async function mutate(payload: UpdateCalendarProposalInput): Promise<void> {
    const owner = epoch,
      id = proposalId.value
    pending.value = true
    error.value = null
    try {
      const updated = await updateCalendarProposal(id, payload)
      if (disposed || owner !== epoch) return
      adopt(updated)
      const read = await getCalendarProposal(id)
      if (!disposed && owner === epoch && read.version >= updated.version)
        adopt(read)
    } catch (cause) {
      if (!disposed && owner === epoch) failure(cause)
    } finally {
      if (!disposed && owner === epoch) pending.value = false
    }
  }
  async function save(): Promise<void> {
    if (locked.value || sourceDirty.value || !proposal.value) return
    try {
      await mutate({ version: proposal.value.version, ...input() })
    } catch (cause) {
      failure(cause)
    }
  }
  /** @param kind 用户单独确认的字段组；calendar 精确包含本次选择的连接和日历。 */
  async function confirm(kind: CalendarConfirmation): Promise<void> {
    if (
      locked.value ||
      fieldsDirty.value ||
      !proposal.value ||
      (kind !== 'calendar' && sourceDirty.value)
    )
      return
    const confirmation: CalendarConfirmationInput =
      kind === 'calendar'
        ? {
            kind,
            connection_id: form.connection_id,
            calendar_id: form.calendar_id,
          }
        : { kind }
    await mutate({ version: proposal.value.version, confirmation })
  }
  /**
   * 候选查询会保存新的不可变提案版本，因此必须 GET 读取同版本或更新版本的完整状态。
   * 回执只证明版本已推进，不能据此推断确认项；读取失败时锁定旧 CAS，显式刷新后方可编辑。
   */
  async function suggest(): Promise<void> {
    if (locked.value || dirty.value || !proposal.value) return
    const owner = epoch,
      id = proposalId.value,
      version = proposal.value.version,
      key = currentKey.value
    pending.value = true
    error.value = null
    try {
      const value = await suggestCalendarTimes(id, { version })
      if (disposed || owner !== epoch) return
      refreshRequired.value = true
      availability.value = null
      evidenceKey.value = ''
      if (
        value.version <= version ||
        proposal.value?.version !== version ||
        key !== currentKey.value
      )
        throw new Error('Invalid calendar candidate version')
      const latest = await getCalendarProposal(id)
      if (disposed || owner !== epoch) return
      if (latest.version < value.version)
        throw new Error('Calendar candidate version is not yet readable')
      // 更晚的版本可能已清除候选，只展示该 GET 自身绑定的候选，不能把早先回执覆盖进去。
      adopt(latest)
    } catch (cause) {
      if (!disposed && owner === epoch) {
        const recovery = actionRecovery(cause)
        error.value = refreshRequired.value
          ? {
            message: '候选已保存，请重新加载最新提案后继续编辑。',
            traceId: recovery.traceId,
            action: 'reload',
          }
          : recovery
      }
    } finally {
      if (!disposed && owner === epoch) pending.value = false
    }
  }
  /** 候选选择只改输入，随后必须保存并重新确认时间；不触发 PATCH 或审批。 */
  function choose(candidate: { starts_at: string; ends_at: string }): void {
    if (locked.value || form.all_day) return
    form.starts_at = instantToWall(candidate.starts_at, form.timezone)
    form.ends_at = instantToWall(candidate.ends_at, form.timezone)
  }
  async function submit(): Promise<void> {
    if (!canSubmit.value || !proposal.value) return
    const owner = epoch,
      version = proposal.value.version
    if (submitIntent?.version !== version)
      submitIntent = { version, intent: createRequestIntent() }
    pending.value = true
    error.value = null
    try {
      const receipt = await submitCalendarProposal(
        proposalId.value,
        version,
        submitIntent.intent,
      )
      if (!disposed && owner === epoch) await submitted(receipt.task_id)
    } catch (cause) {
      if (!disposed && owner === epoch) failure(cause)
    } finally {
      if (!disposed && owner === epoch) pending.value = false
    }
  }
  return {
    proposal,
    form,
    loading,
    busy,
    locked,
    error,
    dirty,
    fieldsDirty,
    sourceDirty,
    conflicts,
    candidates,
    after,
    canSubmit,
    reload,
    save,
    confirm,
    suggest,
    choose,
    submit,
  }
}

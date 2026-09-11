import { computed, onUnmounted, reactive, ref, watch, type Ref } from 'vue'
import {
  generateMailDraft,
  getMailDraft,
  submitMailDraft,
  updateMailDraft,
  type UpdateMailDraftInput,
} from '@/api/mail'
import { createRequestIntent, type RequestIntent } from '@/api/requestIntent'
import type { MailDraft } from '@/api/types'
import { useTaskEvents } from '@/composables/useTaskEvents'
import { useTasksStore } from '@/stores/tasks'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'
import {
  EditorInputError,
  recipientList,
  validateRecipientGroups,
} from '@/features/actions/editorInput'

/** 表单只是当前页面的用户输入；冻结版本、来源绑定及不可变正文由服务端持久化。 */
export interface MailEditorForm {
  connection_id: string
  to: string
  cc: string
  bcc: string
  subject: string
  body_text: string
}

/**
 * @param draftId 路由中的已知本地草稿 ID。
 * @param submitted 只在服务端返回 task_id 后导航到权威操作快照。
 * @returns 版本感知的编辑操作；切换路由、卸载及晚到响应不能覆盖新对象。
 */
export function useMailDraftEditor(
  draftId: Ref<string>,
  submitted: (taskId: string) => Promise<unknown>,
) {
  const draft = ref<MailDraft | null>(null)
  const form = reactive<MailEditorForm>({
    connection_id: '',
    to: '',
    cc: '',
    bcc: '',
    subject: '',
    body_text: '',
  })
  const baseline = ref(''),
    loading = ref(false),
    pending = ref(false),
    error = ref<ActionRecovery | null>(null)
  const instruction = ref(''),
    generationTaskId = ref<string | null>(null)
  const tasks = useTasksStore()
  const generationConnection = useTaskEvents(generationTaskId)
  const generationTask = computed(() =>
    generationTaskId.value ? tasks.tasks[generationTaskId.value] : undefined,
  )
  const generationRunning = computed(
    () =>
      Boolean(generationTaskId.value) &&
      !['succeeded', 'failed', 'cancelled'].includes(
        generationTask.value?.status ?? 'queued',
      ),
  )
  const generationFailed = computed(() =>
    ['failed', 'cancelled'].includes(generationTask.value?.status ?? ''),
  )
  const busy = computed(
    () => loading.value || pending.value || generationRunning.value,
  )
  const locked = computed(() => busy.value || draft.value?.status !== 'editing')
  const dirty = computed(() => baseline.value !== JSON.stringify(form))
  let epoch = 0,
    disposed = false
  let submitIntent: { version: number; intent: RequestIntent } | null = null
  let generationIntent: { input: string; intent: RequestIntent } | null = null
  onUnmounted(() => {
    disposed = true
    epoch += 1
    instruction.value = ''
    draft.value = null
    form.body_text = ''
  })

  /** 应用完整服务端版本；不从模型 delta 拼接正文，不使旧审批继续有效。 */
  function adopt(value: MailDraft): void {
    draft.value = value
    Object.assign(form, {
      connection_id: value.connection_id,
      to: value.to.join(', '),
      cc: value.cc.join(', '),
      bcc: value.bcc.join(', '),
      subject: value.subject,
      body_text: value.body_text,
    })
    baseline.value = JSON.stringify(form)
  }
  /** GET 可恢复最新版本；显式刷新丢弃当前尚未保存的页面输入。 */
  async function reload(): Promise<void> {
    const owner = ++epoch,
      id = draftId.value
    if (!id) {
      draft.value = null
      return
    }
    loading.value = true
    error.value = null
    try {
      const value = await getMailDraft(id)
      if (!disposed && owner === epoch) adopt(value)
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === epoch) loading.value = false
    }
  }
  watch(
    draftId,
    () => {
      draft.value = null
      pending.value = false
      baseline.value = ''
      instruction.value = ''
      generationTaskId.value = null
      submitIntent = null
      generationIntent = null
      void reload()
    },
    { immediate: true },
  )
  watch(
    () => generationTask.value?.status,
    (status, previous) => {
      // 只有持久任务明确终止后才结束旧意图；网络结果未知时仍复用原键，不自动触发新任务。
      if (status && ['succeeded', 'failed', 'cancelled'].includes(status))
        generationIntent = null
      if (status === 'succeeded' && previous !== 'succeeded') void reload()
    },
  )

  /** 构建当前精确输入；回复必须省略 subject 和 connection_id，不给服务端制造重绑歧义。 */
  function input(): UpdateMailDraftInput {
    const value = draft.value
    if (!value) throw new EditorInputError('草稿尚未加载，请重新加载。')
    const to = recipientList(form.to),
      cc = recipientList(form.cc),
      bcc = recipientList(form.bcc)
    validateRecipientGroups(to, cc, bcc)
    return {
      version: value.version,
      to,
      cc,
      bcc,
      body_text: form.body_text,
      ...(value.mode === 'new'
        ? { subject: form.subject, connection_id: form.connection_id }
        : {}),
    }
  }
  const canSubmit = computed(() => {
    if (locked.value || dirty.value) return false
    try {
      return (input().to?.length ?? 0) > 0
    } catch {
      return false
    }
  })
  /** 所有错误先转固定恢复文案；输入验证异常不包含用户输入。 */
  function failure(cause: unknown): void {
    error.value =
      cause instanceof EditorInputError
        ? { message: cause.message, traceId: null, action: 'retry' }
        : actionRecovery(cause)
  }
  /** 保存产生不可变新版本；不会顺带生成审批。 */
  async function save(): Promise<void> {
    if (locked.value) return
    const owner = epoch,
      id = draftId.value
    pending.value = true
    error.value = null
    try {
      const value = await updateMailDraft(id, input())
      if (!disposed && owner === epoch) adopt(value)
    } catch (cause) {
      if (!disposed && owner === epoch) failure(cause)
    } finally {
      if (!disposed && owner === epoch) pending.value = false
    }
  }
  /** 只提交当前已保存版本；网络重试复用原 intent，版本变化则建立新的用户意图。 */
  async function submit(): Promise<void> {
    if (!canSubmit.value || !draft.value) return
    const owner = epoch,
      version = draft.value.version
    if (submitIntent?.version !== version)
      submitIntent = { version, intent: createRequestIntent() }
    pending.value = true
    error.value = null
    try {
      const receipt = await submitMailDraft(
        draftId.value,
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
  /** body-only 草拟使用真实任务进度；生成期间锁定编辑，完成后重新读取不可变正文版本。 */
  async function generate(): Promise<void> {
    if (
      locked.value ||
      dirty.value ||
      !draft.value ||
      !instruction.value.trim()
    )
      return
    const owner = epoch,
      payload = {
        version: draft.value.version,
        instruction: instruction.value.trim(),
      }
    const identity = JSON.stringify(payload)
    if (generationIntent?.input !== identity)
      generationIntent = { input: identity, intent: createRequestIntent() }
    pending.value = true
    error.value = null
    try {
      const receipt = await generateMailDraft(
        draftId.value,
        payload,
        generationIntent.intent,
      )
      if (!disposed && owner === epoch) generationTaskId.value = receipt.task_id
    } catch (cause) {
      if (!disposed && owner === epoch) failure(cause)
    } finally {
      if (!disposed && owner === epoch) pending.value = false
    }
  }
  return {
    draft,
    form,
    instruction,
    dirty,
    busy,
    locked,
    loading,
    error,
    canSubmit,
    generationRunning,
    generationFailed,
    generationConnection,
    reload,
    save,
    submit,
    generate,
  }
}

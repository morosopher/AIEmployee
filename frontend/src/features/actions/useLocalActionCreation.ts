import { onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { createMailDraft, type CreateMailDraftInput } from '@/api/mail'
import {
  createCalendarProposal,
  type CreateCalendarProposalInput,
} from '@/api/calendar'
import { createRequestIntent, type RequestIntent } from '@/api/requestIntent'
import { actionRecovery, type ActionRecovery } from './recovery'

/**
 * 显式创建本地编辑对象的共享入口；只接受固定 new/reply/update 路径，不提交审批。
 * @returns 创建操作和恢复状态；同一载荷的运输失败保留原 intent，成功后才开始新意图。
 */
export function useLocalActionCreation() {
  const router = useRouter()
  const busy = ref(false),
    error = ref<ActionRecovery | null>(null)
  const intents = new Map<string, RequestIntent>()
  let disposed = false
  onUnmounted(() => {
    disposed = true
    intents.clear()
  })

  /** 载荷只含模式或本地来源 ID；新建页面永远等待服务端实际返回对象 ID。 */
  async function create(
    kind: 'mail' | 'calendar',
    input: CreateMailDraftInput | CreateCalendarProposalInput,
  ): Promise<void> {
    if (busy.value) return
    const identity = JSON.stringify([kind, input])
    const intent = intents.get(identity) ?? createRequestIntent()
    intents.set(identity, intent)
    busy.value = true
    error.value = null
    try {
      const result =
        kind === 'mail'
          ? await createMailDraft(input as CreateMailDraftInput, intent)
          : await createCalendarProposal(
              input as CreateCalendarProposalInput,
              intent,
            )
      intents.delete(identity)
      if (!disposed)
        await router.push(
          kind === 'mail'
            ? `/mail/drafts/${result.id}`
            : `/calendar/proposals/${result.id}`,
        )
    } catch (cause) {
      if (!disposed) error.value = actionRecovery(cause)
    } finally {
      if (!disposed) busy.value = false
    }
  }
  return {
    busy,
    error,
    newMail: () => create('mail', { mode: 'new' }),
    newCalendar: () =>
      create('calendar', { operation_kind: 'create', initialization: 'shell' }),
    reply: (threadId: string) =>
      create('mail', { mode: 'reply', source_thread_id: threadId }),
    updateEvent: (eventId: string) =>
      create('calendar', {
        operation_kind: 'update',
        initialization: 'shell',
        event_id: eventId,
      }),
  }
}

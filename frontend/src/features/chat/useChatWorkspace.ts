import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  sendMessage,
} from '@/api/conversations'
import type { Conversation, Message } from '@/api/types'
import { useTaskEvents } from '@/composables/useTaskEvents'
import { useTasksStore } from '@/stores/tasks'
import { useActionsStore } from '@/stores/actions'
import { localTaskId } from '@/features/actions/editorLinks'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'

/**
 * 对话内容与可信动作分开读取，只有恢复出的 trusted_action 任务才能显示审批事实。
 * @returns 会话导航、显式发送和权威快照；模型文本仅作 Markdown 内容，不推导发送/应用状态。
 */
export function useChatWorkspace() {
  const conversation = ref<Conversation | null>(null),
    conversations = ref<Conversation[]>([]),
    messages = ref<Message[]>([])
  const draft = ref(''),
    loading = ref(false),
    sending = ref(false),
    taskId = ref<string | null>(null),
    error = ref<ActionRecovery | null>(null)
  const tasks = useTasksStore(),
    actions = useActionsStore()
  let epoch = 0,
    disposed = false
  let sendIntent: {
    conversationId: string
    content: string
    key: string
  } | null = null
  const task = computed(() =>
    taskId.value ? tasks.tasks[taskId.value] : undefined,
  )
  const trustedId = computed(() =>
    task.value?.kind === 'trusted_action' ? task.value.id : null,
  )
  const action = computed(() =>
    trustedId.value ? (actions.snapshots[trustedId.value] ?? null) : null,
  )
  const connectionState = useTaskEvents(
    taskId,
    (event) => {
      if (event.task_id === trustedId.value) actions.applyEvent(event)
    },
    () => {
      if (trustedId.value) void actions.loadSnapshot(trustedId.value)
      void reloadMessages()
    },
  )
  onUnmounted(() => {
    disposed = true
    epoch += 1
    draft.value = ''
    messages.value = []
    sendIntent = null
  })
  watch(trustedId, (id) => {
    if (id) void actions.loadSnapshot(id)
  })
  watch(
    () => task.value?.status,
    (status) => {
      if (
        status === 'succeeded' ||
        status === 'failed' ||
        status === 'cancelled'
      )
        void reloadMessages()
    },
  )

  /** 切换会话后拒绝旧响应；服务端完整消息覆盖临时结果，不能从 delta 推测执行结果。 */
  async function reloadMessages(): Promise<void> {
    const id = conversation.value?.id,
      owner = epoch
    if (!id) return
    try {
      const result = await getConversation(id)
      if (disposed || owner !== epoch) return
      if (result.conversation.id !== id)
        throw new Error('Conversation identity changed')
      messages.value = result.messages
      if (!taskId.value)
        taskId.value = localTaskId(
          [...result.messages].reverse().find((message) => message.task_id)
            ?.task_id,
        )
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    }
  }
  /** @param value 用户选择的服务端会话。 @returns 新会话只读内容，先关闭旧任务流。 */
  async function selectConversation(value: Conversation): Promise<void> {
    epoch += 1
    const owner = epoch
    conversation.value = value
    messages.value = []
    taskId.value = null
    error.value = null
    loading.value = true
    sending.value = false
    await reloadMessages()
    if (!disposed && owner === epoch) loading.value = false
  }
  /** 只在用户明确点击发送后提交文本；网络失败保留文本及同一 client_request_id。 */
  async function submit(): Promise<void> {
    if (
      sending.value ||
      loading.value ||
      !conversation.value ||
      !draft.value.trim()
    )
      return
    const id = conversation.value.id,
      content = draft.value,
      owner = epoch
    if (sendIntent?.conversationId !== id || sendIntent.content !== content)
      sendIntent = { conversationId: id, content, key: crypto.randomUUID() }
    sending.value = true
    error.value = null
    try {
      const receipt = await sendMessage(id, content, sendIntent.key)
      if (disposed || owner !== epoch) return
      const accepted = localTaskId(receipt.task_id)
      if (!accepted) throw new Error('Invalid accepted chat task')
      taskId.value = accepted
      sendIntent = null
      if (draft.value === content) draft.value = ''
      await reloadMessages()
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === epoch) sending.value = false
    }
  }
  /** 删除沿用明确的人工作用域，先确认再调用本地会话接口，不执行供应商动作。 */
  async function removeConversation(value: Conversation): Promise<void> {
    if (!window.confirm('确定删除会话？')) return
    try {
      await deleteConversation(value.id)
      if (disposed) return
      conversations.value = conversations.value.filter(
        (item) => item.id !== value.id,
      )
      if (conversation.value?.id === value.id) {
        epoch += 1
        conversation.value = null
        messages.value = []
        taskId.value = null
        const next = conversations.value[0]
        if (next) await selectConversation(next)
      }
    } catch (cause) {
      if (!disposed) error.value = actionRecovery(cause)
    }
  }
  async function load(): Promise<void> {
    const owner = ++epoch
    loading.value = true
    error.value = null
    try {
      const available = await listConversations()
      if (disposed || owner !== epoch) return
      const initial = available[0] ?? (await createConversation())
      if (disposed || owner !== epoch) return
      conversations.value = available.length ? available : [initial]
      await selectConversation(initial)
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === epoch) loading.value = false
    }
  }
  onMounted(() => {
    void load()
  })
  return {
    conversation,
    conversations,
    messages,
    draft,
    loading,
    sending,
    error,
    task,
    action,
    connectionState,
    actions,
    trustedId,
    load,
    reloadMessages,
    selectConversation,
    removeConversation,
    submit,
  }
}

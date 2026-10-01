<script setup lang="ts">
import { ref } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ScrollPanel from 'primevue/scrollpanel'
import Textarea from 'primevue/textarea'
import { useRouter } from 'vue-router'
import MarkdownMessage from '@/components/MarkdownMessage.vue'
import ActionDetail from '@/components/ActionDetail.vue'
import { useChatWorkspace } from '@/features/chat/useChatWorkspace'
import { focusedEditorPath } from '@/features/actions/editorLinks'
import { actionStatusLabel } from '@/features/actions/presentation'
import { useAuthStore } from '@/stores/auth'

/** 聊天只呈现服务端消息与任务快照；任何真实操作均需在聚焦编辑器审阅并提交审批。 */
const router = useRouter(),
  auth = useAuthStore()
const {
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
} = useChatWorkspace()
/** 只记录本地输入法会话，不持久化消息或业务状态。 */
const composing = ref(false)

/**
 * 键盘仅触发现有 submit 端口；组合输入和 Shift+Enter 保留浏览器默认行为。
 * Safari 的输入法确认可能仅报告 keyCode 229，因此同时检查原生标记与组合会话状态。
 * @param event 消息输入框的键盘事件。
 */
function onMessageKeydown(event: KeyboardEvent): void {
  if (
    event.key !== 'Enter' ||
    event.shiftKey ||
    event.isComposing ||
    composing.value ||
    event.keyCode === 229
  )
    return
  event.preventDefault()
  void submit()
}

/** @param value 已经过 Markdown 清洗的本地路径；再次收窄后只导航，不创建或批准动作。 */
async function openEditor(value: string): Promise<void> {
  const path = focusedEditorPath(value)
  if (path) await router.push(path)
}
</script>
<template>
  <section class="min-w-0 space-y-4 wrap-anywhere">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h1 class="text-2xl font-semibold">
        聊天
      </h1>
      <Button
        as="router-link"
        to="/actions"
        label="操作中心"
        severity="secondary"
      />
    </div>
    <p
      v-if="loading"
      role="status"
    >
      正在加载会话…
    </p>
    <!-- Message 的根节点承接原有 alert，保留错误文案和人工重载动作。 -->
    <Message
      v-if="error"
      severity="error"
      role="alert"
    >
      <div class="space-y-2">
        <p>
          {{ error.message
          }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
        </p>
        <Button
          label="重新加载会话"
          severity="secondary"
          @click="load"
        />
      </div>
    </Message>
    <nav
      aria-label="会话历史"
      class="flex flex-wrap gap-2"
    >
      <div
        v-for="item in conversations"
        :key="item.id"
        class="flex items-center gap-1"
      >
        <Button
          :label="item.title"
          severity="secondary"
          outlined
          @click="selectConversation(item)"
        />
        <Button
          label="删除"
          severity="danger"
          text
          @click="removeConversation(item)"
        />
      </div>
    </nav>
    <!-- 滚动容器只包裹 REST 最终消息；不新增 delta 投影或实时连接。 -->
    <ScrollPanel
      v-if="messages.length"
      class="h-80 w-full md:h-96"
      role="region"
      aria-label="对话消息"
    >
      <div class="space-y-4 pr-4">
        <div
          v-for="message in messages"
          :key="message.id"
          class="rounded-xl border border-surface bg-surface-0 p-4"
        >
          <MarkdownMessage
            :content="message.content_markdown"
            :open-editor="openEditor"
          />
        </div>
      </div>
    </ScrollPanel>
    <!-- 原任务 status 与断线后缀合为一个 Message 根节点，覆盖内置 alert，避免重复播报。 -->
    <Message
      v-if="task"
      data-testid="chat-task-status"
      :severity="connectionState === 'connected' ? 'info' : 'warn'"
      role="status"
      aria-live="polite"
    >
      任务：{{ actionStatusLabel(task.status)
      }}{{
        connectionState === 'connected' ? '' : ' · 正在恢复实时连接'
      }}
    </Message>
    <Message
      v-if="trustedId && actions.snapshotErrors[trustedId]"
      severity="error"
      role="alert"
    >
      操作快照读取失败，请在操作中心重新加载。
    </Message>
    <ActionDetail
      v-if="action"
      :snapshot="action"
      :timezone="auth.user?.timezone ?? 'UTC'"
      @changed="reloadMessages"
    />
    <p class="text-sm text-muted-color">
      邮件发送与日程执行结果，以操作中心的服务端记录为准。草稿或提案链接只打开编辑器。
    </p>
    <!-- 提交、幂等及错误恢复沿用 feature；页面只负责输入法与键盘交互。 -->
    <form
      class="space-y-3 rounded-xl border border-surface bg-surface-0 p-4"
      @submit.prevent="submit"
    >
      <label
        for="chat-message"
        class="block font-medium"
      >消息</label>
      <Textarea
        id="chat-message"
        v-model="draft"
        auto-resize
        rows="3"
        class="w-full"
        aria-describedby="chat-keyboard-help"
        :disabled="sending"
        @keydown="onMessageKeydown"
        @compositionstart="composing = true"
        @compositionend="composing = false"
      />
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p
          id="chat-keyboard-help"
          class="text-sm text-muted-color"
        >
          Enter 发送，Shift+Enter 换行
        </p>
        <Button
          type="submit"
          :label="sending ? '正在发送…' : '发送'"
          :loading="sending"
          :aria-busy="sending"
          :disabled="sending || loading || !draft.trim()"
        />
      </div>
    </form>
  </section>
</template>

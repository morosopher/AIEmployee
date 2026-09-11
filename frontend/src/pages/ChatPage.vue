<script setup lang="ts">
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
async function openEditor(value: string): Promise<void> {
  const path = focusedEditorPath(value)
  if (path) await router.push(path)
}
</script>
<template>
  <section class="chat-page">
    <h1>聊天</h1>
    <p
      v-if="loading"
      role="status"
    >
      正在加载会话…
    </p>
    <div
      v-if="error"
      role="alert"
    >
      <p>
        {{ error.message
        }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
      </p>
      <button
        type="button"
        @click="load"
      >
        重新加载会话
      </button>
    </div>
    <nav aria-label="会话历史">
      <div
        v-for="item in conversations"
        :key="item.id"
      >
        <button
          type="button"
          @click="selectConversation(item)"
        >
          {{ item.title }}
        </button><button
          type="button"
          @click="removeConversation(item)"
        >
          删除
        </button>
      </div>
    </nav>
    <div
      v-for="message in messages"
      :key="message.id"
    >
      <MarkdownMessage
        :content="message.content_markdown"
        :open-editor="openEditor"
      />
    </div>
    <p
      v-if="task"
      data-testid="chat-task-status"
      role="status"
    >
      任务：{{ actionStatusLabel(task.status)
      }}{{ connectionState === 'connected' ? '' : ' · 正在恢复实时连接' }}
    </p>
    <p
      v-if="trustedId && actions.snapshotErrors[trustedId]"
      role="alert"
    >
      操作快照读取失败，请在操作中心重新加载。
    </p>
    <ActionDetail
      v-if="action"
      :snapshot="action"
      :timezone="auth.user?.timezone ?? 'UTC'"
      @changed="reloadMessages"
    />
    <p>
      邮件发送与日程执行结果，以操作中心的服务端记录为准。草稿或提案链接只打开编辑器。
    </p>
    <form @submit.prevent="submit">
      <label>消息<textarea
        v-model="draft"
        aria-label="消息"
        :disabled="sending"
      /></label><button
        type="submit"
        :disabled="sending || loading || !draft.trim()"
      >
        {{ sending ? '正在发送…' : '发送' }}
      </button>
    </form>
  </section>
</template>
<style scoped>
.chat-page {
  min-width: 0;
  overflow-wrap: anywhere;
}
textarea {
  display: block;
  width: 100%;
  box-sizing: border-box;
  min-height: 7rem;
  font: inherit;
}
button:focus-visible,
textarea:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

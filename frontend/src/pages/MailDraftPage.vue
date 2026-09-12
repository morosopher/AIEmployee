<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useConnectionCatalog } from '@/composables/useConnectionCatalog'
import { useMailDraftEditor } from '@/features/mail/useMailDraftEditor'
import { useLocalActionCreation } from '@/features/actions/useLocalActionCreation'
import { actionStatusLabel } from '@/features/actions/presentation'
import LocalEditorFrame from '@/components/LocalEditorFrame.vue'
import MailDraftFields from '@/components/MailDraftFields.vue'
import EditorRecovery from '@/components/EditorRecovery.vue'
import LinkedActionPanel from '@/components/LinkedActionPanel.vue'

/** 专注本地邮件编辑；页面仅组合输入、目录、任务事实与受保护导航。 */
const route = useRoute(),
  router = useRouter()
const draftId = computed(() =>
  typeof route.params.draftId === 'string' ? route.params.draftId : '',
)
const {
  entries,
  loading: catalogLoading,
  error: catalogError,
  load: loadCatalog,
} = useConnectionCatalog()
const {
  draft,
  form,
  instruction,
  dirty,
  busy,
  locked,
  loading,
  error,
  canSubmit,
  recipientSummary,
  generationRunning,
  generationFailed,
  generationConnection,
  reload,
  save,
  submit,
  generate,
} = useMailDraftEditor(draftId, (taskId) =>
  router.push({ path: '/actions', query: { task: taskId } }),
)
const {
  newMail,
  error: creationError,
  busy: creating,
} = useLocalActionCreation()
const heading = computed(() =>
  draft.value?.mode === 'reply'
    ? '回复邮件'
    : draft.value?.mode === 'reply_all'
      ? '全部回复'
      : '邮件草稿',
)
onMounted(() => {
  void loadCatalog()
})
</script>
<template>
  <LocalEditorFrame :title="heading">
    <RouterLink to="/actions">
      返回操作中心
    </RouterLink>
    <p class="editor-note">
      草稿只保存在本地。发送不可撤回；保存后请审阅精确版本，再提交人工审批。
    </p>
    <p
      v-if="loading || catalogLoading"
      role="status"
    >
      正在加载草稿与账户…
    </p>
    <EditorRecovery
      :error="error || catalogError || creationError"
      :busy="busy || creating"
      @reload="reload"
      @new-object="newMail"
    />
    <button
      type="button"
      name="reload-editor"
      :disabled="busy"
      @click="reload"
    >
      重新加载草稿
    </button>
    <template v-if="draft">
      <p>版本 {{ draft.version }} · {{ actionStatusLabel(draft.status) }}</p>
      <MailDraftFields
        :model-value="form"
        :draft="draft"
        :entries="entries"
        :disabled="locked"
        :recipient-summary="recipientSummary"
      />
      <p
        v-if="dirty"
        role="status"
      >
        有未保存的输入，请先保存后审阅。
      </p>
      <div class="editor-controls">
        <button
          type="button"
          name="save-draft"
          :disabled="locked || !dirty"
          @click="save"
        >
          保存草稿
        </button><button
          type="button"
          name="submit-draft"
          :disabled="!canSubmit"
          @click="submit"
        >
          提交审批
        </button>
      </div>
      <p v-if="!draft.to.length">
        提交审批前至少填写一位收件人。
      </p>
      <details>
        <summary>使用模型草拟正文</summary>
        <p>请先保存当前版本。模型只提供待审阅正文。</p>
        <label>草拟要求<textarea
          v-model="instruction"
          aria-label="草拟要求"
          :disabled="locked"
          maxlength="2000"
        /></label><button
          type="button"
          name="generate-draft"
          :disabled="locked || dirty || !instruction.trim()"
          @click="generate"
        >
          草拟正文
        </button>
      </details>
      <p
        v-if="generationRunning"
        role="status"
      >
        正在草拟，请等待服务端保存结果。{{
          generationConnection === 'connected'
            ? ''
            : '连接恢复中，任务状态以服务端为准。'
        }}
      </p>
      <p
        v-if="generationFailed"
        role="alert"
      >
        草拟失败，原草稿仍保留。请检查任务历史并重新加载后再试。
      </p>
      <p
        v-if="draft.status !== 'editing'"
        class="editor-note"
      >
        当前版本不可编辑。待审批时，请先撤回审批任务再修改。
      </p>
      <LinkedActionPanel
        :local-id="draft.id"
        item-kind="mail_draft"
        :task-query="route.query.task"
        @changed="reload"
      />
    </template>
  </LocalEditorFrame>
</template>

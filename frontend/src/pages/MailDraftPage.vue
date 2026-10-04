<script setup lang="ts">
import {
  computed,
  onMounted,
  onUnmounted,
  defineAsyncComponent,
  defineComponent,
  h,
  ref,
} from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { useConfirm } from 'primevue/useconfirm'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useConnectionCatalog } from '@/composables/useConnectionCatalog'
import { useMailDraftEditor } from '@/features/mail/useMailDraftEditor'
import { useLocalActionCreation } from '@/features/actions/useLocalActionCreation'
import { actionStatusLabel } from '@/features/actions/presentation'
import LocalEditorFrame from '@/components/LocalEditorFrame.vue'

import EditorRecovery from '@/components/EditorRecovery.vue'
import LinkedActionPanel from '@/components/LinkedActionPanel.vue'

/** Form/zod/AutoComplete 在打开邮件页后加载，保持既有入口与路由契约，失败只允许显式重试。 */
const moduleFailed = ref(false)
const MailEditorForm = defineAsyncComponent({
  loader: () => import('@/components/MailEditorForm.vue'),
  delay: 0,
  loadingComponent: defineComponent({
    setup: () => () =>
      moduleFailed.value
        ? null
        : h('div', { class: 'space-y-2' }, [
            h(
              Message,
              {
                role: 'status',
                severity: 'secondary',
                'aria-live': 'polite',
                class: 'text-color',
              },
              () => '正在加载邮件表单…',
            ),
            h(Skeleton, { height: '12rem' }),
          ]),
  }),
  onError() {
    moduleFailed.value = true
  },
})
/** 模块加载失败后显式整页重试，原 hook 按既有规则重新取得服务端草稿。 */
function retryForm(): void {
  window.location.reload()
}

/**
 * 专注本地邮件编辑；页面仅组合输入、目录、任务事实与受保护导航。
 * Live region：原加载 status 留在页面；未保存、生成状态及失败逐字移入 MailEditorForm。
 * 新增互斥的异步模块加载 status／失败 alert；未保存离开复用外壳 ConfirmDialog，不重复挂载。
 */
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
const confirmation = useConfirm()
let settleLeave: ((accepted: boolean) => void) | null = null
/** 关闭或后续导航只结算当前确认；不把关闭视为批准。 */
function finishLeave(accepted: boolean): void {
  const settle = settleLeave
  settleLeave = null
  settle?.(accepted)
}
/** 页面只提示丢弃本地输入；认证跳登录继续沿用原会话和卸载清理边界。 */
const removeLeaveGuard = router.beforeEach((to, from) => {
  if (!dirty.value || to.path === from.path || to.path === '/login') return true
  finishLeave(false)
  return new Promise<boolean>((resolve) => {
    settleLeave = resolve
    confirmation.require({
      header: '离开邮件编辑器',
      message: '草稿尚未保存，确定离开？',
      defaultFocus: 'reject',
      rejectProps: { label: '继续编辑', severity: 'secondary', outlined: true },
      acceptProps: { label: '放弃修改并离开' },
      accept: () => finishLeave(true),
      reject: () => finishLeave(false),
      onHide: () => finishLeave(false),
    })
  })
})
/** 浏览器卸载只能使用原生固定提示，不提供正文摘录，也不写浏览器存储。 */
function beforeUnload(event: BeforeUnloadEvent): void {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
onMounted(() => {
  void loadCatalog()
  window.addEventListener('beforeunload', beforeUnload)
})
onUnmounted(() => {
  removeLeaveGuard()
  window.removeEventListener('beforeunload', beforeUnload)
  if (settleLeave) {
    finishLeave(false)
    confirmation.close()
  }
})
</script>
<template>
  <LocalEditorFrame :title="heading">
    <RouterLink
      to="/actions"
      class="text-primary underline"
    >
      返回操作中心
    </RouterLink>
    <p class="rounded-lg border-l-4 border-orange-600 bg-orange-50 p-3">
      草稿只保存在本地。发送不可撤回；保存后请审阅精确版本，再提交人工审批。
    </p>
    <div
      v-if="loading || catalogLoading"
      class="space-y-2"
    >
      <Message
        role="status"
        aria-live="polite"
        severity="secondary"
      >
        正在加载草稿与账户…
      </Message>
      <Skeleton height="3rem" />
    </div>
    <EditorRecovery
      v-if="!draft"
      :error="error || catalogError || creationError"
      :busy="busy || creating"
      @reload="reload"
      @new-object="newMail"
    />
    <Button
      type="button"
      name="reload-editor"
      class="text-color"
      label="重新加载草稿"
      severity="secondary"
      outlined
      :disabled="busy"
      @click="reload"
    />
    <template v-if="draft">
      <p>版本 {{ draft.version }} · {{ actionStatusLabel(draft.status) }}</p>
      <MailEditorForm
        v-model:instruction="instruction"
        :model-value="form"
        :draft="draft"
        :entries="entries"
        :dirty="dirty"
        :busy="busy"
        :locked="locked"
        :can-submit="canSubmit"
        :recipient-summary="recipientSummary"
        :generation-running="generationRunning"
        :generation-failed="generationFailed"
        :generation-connection="generationConnection"
        :error="error || catalogError || creationError"
        :creating="creating"
        @save="save"
        @submit="submit"
        @generate="generate"
        @reload="reload"
        @new-object="newMail"
      />
      <Message
        v-if="moduleFailed"
        severity="error"
      >
        邮件表单加载失败，请重试。重试会重新加载页面。
        <Button
          type="button"
          label="重试加载表单"
          severity="secondary"
          @click="retryForm"
        />
      </Message>
      <LinkedActionPanel
        :local-id="draft.id"
        item-kind="mail_draft"
        :task-query="route.query.task"
        @changed="reload"
      />
    </template>
  </LocalEditorFrame>
</template>

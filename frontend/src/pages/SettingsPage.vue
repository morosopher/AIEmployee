<script setup lang="ts">
import {
  onMounted,
  onUnmounted,
  ref,
  computed,
  defineAsyncComponent,
  defineComponent,
  h,
} from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import { useConfirm } from 'primevue/useconfirm'
import { listSessions, revokeSession } from '@/api/auth'
import {
  requestAllDataDeletion,
  requestSourceCacheDeletion,
} from '@/api/privacy'
import type { Session } from '@/api/types'
import { getCurrentUser, getTask, ProblemError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { RouterLink, useRouter } from 'vue-router'

/** 页面浮层只通知外壳隔离背景，外壳负责动画结束后的焦点归还。 */
const emit = defineEmits<{ 'modal-change': [open: boolean] }>()
const moduleFailed = ref(false)
/** Form/zod/DatePicker 在设置页才下载；失败必须有真实重试入口，不把失败变成空白。 */
const WorkSettingsForm = defineAsyncComponent({
  loader: () => import('@/components/WorkSettingsForm.vue'),
  delay: 0,
  loadingComponent: defineComponent({
    setup: () => () =>
      moduleFailed.value
        ? null
        : h('div', { class: 'space-y-2' }, [
            h(
              Message,
              { severity: 'secondary', role: 'status', 'aria-live': 'polite' },
              () => '正在加载工作设置表单…',
            ),
            h(Skeleton, { height: '3rem' }),
          ]),
  }),
  onError() {
    moduleFailed.value = true
  },
})
/** 浏览器缓存失败的 ES 模块导入；明确整页重载后重试，避免假装重新发出了请求。 */
function retryForm(): void {
  window.location.reload()
}
const allDataDialogOpen = ref(false)
const workSettingsDirty = ref(false)
const confirmation = useConfirm()
let settleLeave: ((accepted: boolean) => void) | null = null
/** 同意、拒绝、Esc 与卸载只结算一次；提示不包含用户正文或字段摘录。 */
function finishLeave(accepted: boolean): void {
  const settle = settleLeave
  settleLeave = null
  settle?.(accepted)
}
/** 浏览器关闭只能使用原生提示，不能保证浏览器一定显示自定义确认框。 */
function beforeUnload(event: BeforeUnloadEvent): void {
  if (workSettingsDirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
/** 工作设置交由专用组件；此页面保留已验证的隐私删除和会话生命周期。 */
const sessions = ref<Session[]>([])
const error = ref<string | null>(null)
const deletionError = ref<string | null>(null)
const allDataConfirmation = ref('')
const sourceDeletionTaskId = ref<string | null>(null)
const allDataDeletionTaskId = ref<string | null>(null)
const activeDeletionTaskId = ref<string | null>(null)
const activeDeletionKind = ref<'source-cache' | 'all-data' | null>(null)
const deletionSubmitting = ref(false)
const deletionPending = computed(
  () => activeDeletionTaskId.value !== null || deletionSubmitting.value,
)
let deletionMonitor: ReturnType<typeof setInterval> | null = null
let disposed = false
let deletionSubmission = 0
const auth = useAuthStore()
const router = useRouter()
/** 页面存续期间注册离开提示；认证失效跳登录必须继续遵循原会话生命周期。 */
const removeLeaveGuard = router.beforeEach((to, from) => {
  if (!workSettingsDirty.value || to.path === from.path || to.path === '/login')
    return true
  finishLeave(false)
  return new Promise<boolean>((resolve) => {
    settleLeave = resolve
    confirmation.require({
      header: '离开设置页',
      message: '工作设置尚未保存，确定离开？',
      defaultFocus: 'reject',
      rejectProps: { label: '继续编辑', severity: 'secondary', outlined: true },
      acceptProps: { label: '放弃修改并离开' },
      accept: () => finishLeave(true),
      reject: () => finishLeave(false),
      onHide: () => finishLeave(false),
    })
  })
})
/** 仅组合确认 UI 和原删除动作；202 回执后关闭弹窗，轮询仍由原状态机管理。 */
async function confirmAllData(): Promise<void> {
  await deleteAllData()
  if (allDataDeletionTaskId.value) allDataDialogOpen.value = false
}
/** 只读取当前用户活动会话，工作设置失败不抹掉隐私控制的状态。 */
async function load() {
  try {
    sessions.value = await listSessions()
  } catch {
    error.value = '无法加载会话。'
  }
}
async function revoke(id: string) {
  if (!window.confirm('确定撤销该会话？')) return
  try {
    const current =
      sessions.value.find((session) => session.id === id)?.is_current === true
    await revokeSession(id)
    sessions.value = sessions.value.filter((session) => session.id !== id)
    if (current) {
      auth.clear()
      await router.push('/login')
    }
  } catch {
    error.value = '撤销失败。'
  }
}
function idempotencyKey(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`
}
function stopDeletionMonitor(): void {
  if (deletionMonitor !== null) clearInterval(deletionMonitor)
  deletionMonitor = null
}
async function verifyAllDataSession(): Promise<void> {
  try {
    await getCurrentUser()
  } catch (reason: unknown) {
    if (
      !disposed &&
      reason instanceof ProblemError &&
      (reason.problem.status === 401 || reason.problem.status === 404)
    ) {
      activeDeletionTaskId.value = null
      activeDeletionKind.value = null
      stopDeletionMonitor()
      auth.clear()
      await router.push('/login')
    }
  }
}
async function pollDeletionTask(): Promise<void> {
  const taskId = activeDeletionTaskId.value
  if (!taskId || disposed) return
  try {
    const task = await getTask(taskId)
    if (
      disposed ||
      taskId !== activeDeletionTaskId.value ||
      !['succeeded', 'failed', 'cancelled'].includes(task.status)
    )
      return
    const kind = activeDeletionKind.value
    // 服务端删除 TaskRun 与注销 Cookie 并非同一原子响应；成功快照仍须等待会话撤销事实。
    if (task.status === 'succeeded' && kind === 'all-data') {
      await verifyAllDataSession()
      return
    }
    activeDeletionTaskId.value = null
    activeDeletionKind.value = null
    stopDeletionMonitor()
    if (task.status !== 'succeeded') {
      deletionError.value = `删除任务失败（${task.status}）。`
      return
    }
  } catch (reason: unknown) {
    // TaskRun 消失仅说明记录不可读；仍须由会话端点确认注销，避免误退出有效会话。
    if (
      !disposed &&
      activeDeletionKind.value === 'all-data' &&
      reason instanceof ProblemError &&
      (reason.problem.status === 401 || reason.problem.status === 404)
    )
      await verifyAllDataSession()
    // 其余短暂读取失败保留待处理状态并交由下一轮恢复，不能伪造删除完成。
  }
}
function monitorDeletion(
  taskId: string,
  kind: 'source-cache' | 'all-data',
): void {
  if (disposed) return
  activeDeletionTaskId.value = taskId
  activeDeletionKind.value = kind
  stopDeletionMonitor()
  void pollDeletionTask()
  deletionMonitor = setInterval(() => void pollDeletionTask(), 2_000)
}
async function deleteSourceCache(): Promise<void> {
  if (deletionPending.value) return
  const submission = ++deletionSubmission
  deletionSubmitting.value = true
  deletionError.value = null
  try {
    const taskId = (
      await requestSourceCacheDeletion(idempotencyKey('source-cache'))
    ).task_id
    if (!disposed && submission === deletionSubmission) {
      sourceDeletionTaskId.value = taskId
      monitorDeletion(taskId, 'source-cache')
    }
  } catch {
    if (!disposed && submission === deletionSubmission)
      deletionError.value = '来源缓存删除任务创建失败，请稍后重试。'
  } finally {
    if (!disposed && submission === deletionSubmission)
      deletionSubmitting.value = false
  }
}
async function deleteAllData(): Promise<void> {
  if (allDataConfirmation.value !== 'DELETE ALL DATA') {
    deletionError.value = '请输入 DELETE ALL DATA 以确认。'
    return
  }
  if (deletionPending.value) return
  const submission = ++deletionSubmission
  deletionSubmitting.value = true
  deletionError.value = null
  try {
    const taskId = (
      await requestAllDataDeletion(
        allDataConfirmation.value,
        idempotencyKey('all-data'),
      )
    ).task_id
    if (!disposed && submission === deletionSubmission) {
      allDataDeletionTaskId.value = taskId
      monitorDeletion(taskId, 'all-data')
    }
  } catch {
    if (!disposed && submission === deletionSubmission)
      deletionError.value = '全部数据删除任务创建失败，请稍后重试。'
  } finally {
    if (!disposed && submission === deletionSubmission)
      deletionSubmitting.value = false
  }
}
onMounted(() => {
  void load()
  window.addEventListener('beforeunload', beforeUnload)
})
onUnmounted(() => {
  removeLeaveGuard()
  window.removeEventListener('beforeunload', beforeUnload)
  if (settleLeave) {
    finishLeave(false)
    confirmation.close()
  }
  emit('modal-change', false)
  disposed = true
  stopDeletionMonitor()
})
</script>
<template>
  <section class="space-y-6">
    <h1 class="text-2xl font-semibold">
      设置
    </h1>
    <WorkSettingsForm @dirty-change="workSettingsDirty = $event" />
    <Message
      v-if="moduleFailed"
      severity="error"
    >
      工作设置表单加载失败，请重试。重试会重新加载页面。
      <Button
        type="button"
        label="重试加载表单"
        severity="secondary"
        @click="retryForm"
      />
    </Message>
    <Message
      v-if="error"
      severity="error"
    >
      {{ error }}
    </Message>
    <Card>
      <template #title>
        <h2
          id="privacy-heading"
          class="text-lg font-semibold"
        >
          隐私与数据
        </h2>
      </template>
      <template #content>
        <section
          aria-labelledby="privacy-heading"
          class="space-y-4"
        >
          <Button
            type="button"
            label="删除来源缓存"
            severity="secondary"
            outlined
            :disabled="deletionPending"
            :loading="deletionSubmitting && !allDataDialogOpen"
            @click="deleteSourceCache"
          />
          <RouterLink
            v-if="sourceDeletionTaskId"
            :to="{ path: '/tasks', query: { task_id: sourceDeletionTaskId } }"
            class="block text-primary underline"
          >
            查看来源缓存删除任务
          </RouterLink>
          <!-- 确认前说明本地删除边界，不能把未知写结果或本地清理理解为供应商撤回。 -->
          <p id="all-data-deletion-notice">
            删除本地数据不能撤回已发送的邮件或已生效的日程变更。结果未知的操作也可能已在供应商侧生效。
          </p>
          <Button
            type="button"
            label="删除全部数据"
            severity="danger"
            outlined
            :disabled="deletionPending"
            @click="allDataDialogOpen = true"
          />
          <RouterLink
            v-if="allDataDeletionTaskId"
            :to="{ path: '/tasks', query: { task_id: allDataDeletionTaskId } }"
            class="block text-primary underline"
          >
            查看全部数据删除任务
          </RouterLink>
          <Message
            v-if="deletionError && !allDataDialogOpen"
            severity="error"
          >
            {{ deletionError }}
          </Message>
        </section>
      </template>
    </Card>
    <Card>
      <template #title>
        <h2 class="text-lg font-semibold">
          活动会话
        </h2>
      </template>
      <template #content>
        <ul class="space-y-3">
          <li
            v-for="session in sessions"
            :key="session.id"
            class="flex flex-wrap items-center gap-3"
          >
            <span>{{ session.created_at }} · {{ session.expires_at }}</span>
            <Button
              type="button"
              label="撤销"
              severity="secondary"
              outlined
              @click="revoke(session.id)"
            />
          </li>
        </ul>
      </template>
    </Card>
    <Dialog
      v-model:visible="allDataDialogOpen"
      modal
      header="删除全部数据"
      class="mx-4 w-full max-w-xl"
      @show="emit('modal-change', true)"
      @after-hide="emit('modal-change', false)"
    >
      <div class="space-y-4">
        <p id="all-data-confirmation-notice">
          删除本地数据不能撤回已发送的邮件或已生效的日程变更。结果未知的操作也可能已在供应商侧生效。
        </p>
        <p>请输入 DELETE ALL DATA 以确认。</p>
        <div class="grid gap-1">
          <label for="all-data-confirmation">确认全部删除</label>
          <InputText
            id="all-data-confirmation"
            v-model="allDataConfirmation"
            autofocus
            autocomplete="off"
            aria-describedby="all-data-confirmation-notice"
            :disabled="deletionPending"
          />
        </div>
        <Message
          v-if="deletionError"
          severity="error"
        >
          {{
            deletionError
          }}
        </Message>
      </div>
      <template #footer>
        <Button
          type="button"
          label="取消"
          severity="secondary"
          outlined
          @click="allDataDialogOpen = false"
        />
        <Button
          type="button"
          label="确认删除全部数据"
          severity="danger"
          :disabled="deletionPending"
          :loading="deletionSubmitting"
          @click="confirmAllData"
        />
      </template>
    </Dialog>
  </section>
</template>

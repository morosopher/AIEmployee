<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, computed } from 'vue'
import { getSettings, updateSettings } from '@/api/settings'
import { listSessions, revokeSession } from '@/api/auth'
import { requestAllDataDeletion, requestSourceCacheDeletion } from '@/api/privacy'
import type { Session, UserSettings } from '@/api/types'
import { getCurrentUser, getTask, ProblemError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { RouterLink, useRouter } from 'vue-router'

const form = reactive<Partial<UserSettings>>({ timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00', email_body_retention_days: 30, source_metadata_retention_days: 180, workspace_history_retention_days: 365 })
const sessions = ref<Session[]>([])
const saved = ref(false)
const error = ref<string | null>(null)
const deletionError = ref<string | null>(null)
const allDataConfirmation = ref('')
const sourceDeletionTaskId = ref<string | null>(null)
const allDataDeletionTaskId = ref<string | null>(null)
const activeDeletionTaskId = ref<string | null>(null)
const activeDeletionKind = ref<'source-cache' | 'all-data' | null>(null)
const deletionSubmitting = ref(false)
const deletionPending = computed(() => activeDeletionTaskId.value !== null || deletionSubmitting.value)
let deletionMonitor: ReturnType<typeof setInterval> | null = null
let disposed = false
let deletionSubmission = 0
const timezones = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : ['UTC', 'Asia/Shanghai', 'America/Los_Angeles']
const auth = useAuthStore(); const router = useRouter()
async function load() { try { Object.assign(form, await getSettings()); sessions.value = await listSessions() } catch { error.value = '无法加载设置。' } }
async function save() { saved.value = false; error.value = null; try { Object.assign(form, await updateSettings(form)); saved.value = true } catch { error.value = '设置无效，请检查输入。' } }
async function revoke(id: string) { if (!window.confirm('确定撤销该会话？')) return; try { const current = sessions.value.find((session) => session.id === id)?.is_current === true; await revokeSession(id); sessions.value = sessions.value.filter((session) => session.id !== id); if (current) { auth.clear(); await router.push('/login') } } catch { error.value = '撤销失败。' } }
function idempotencyKey(prefix: string): string { return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}` }
function stopDeletionMonitor(): void { if (deletionMonitor !== null) clearInterval(deletionMonitor); deletionMonitor = null }
async function verifyAllDataSession(): Promise<void> {
  try { await getCurrentUser() }
  catch (reason: unknown) { if (!disposed && reason instanceof ProblemError && (reason.problem.status === 401 || reason.problem.status === 404)) { activeDeletionTaskId.value = null; activeDeletionKind.value = null; stopDeletionMonitor(); auth.clear(); await router.push('/login') } }
}
async function pollDeletionTask(): Promise<void> {
  const taskId = activeDeletionTaskId.value
  if (!taskId || disposed) return
  try {
    const task = await getTask(taskId)
    if (disposed || taskId !== activeDeletionTaskId.value || !['succeeded', 'failed', 'cancelled'].includes(task.status)) return
    const kind = activeDeletionKind.value
    // 服务端删除 TaskRun 与注销 Cookie 并非同一原子响应；成功快照仍须等待会话撤销事实。
    if (task.status === 'succeeded' && kind === 'all-data') { await verifyAllDataSession(); return }
    activeDeletionTaskId.value = null; activeDeletionKind.value = null; stopDeletionMonitor()
    if (task.status !== 'succeeded') { deletionError.value = `删除任务失败（${task.status}）。`; return }
  } catch (reason: unknown) {
    // TaskRun 消失仅说明记录不可读；仍须由会话端点确认注销，避免误退出有效会话。
    if (!disposed && activeDeletionKind.value === 'all-data' && reason instanceof ProblemError && (reason.problem.status === 401 || reason.problem.status === 404)) await verifyAllDataSession()
    // 其余短暂读取失败保留待处理状态并交由下一轮恢复，不能伪造删除完成。
  }
}
function monitorDeletion(taskId: string, kind: 'source-cache' | 'all-data'): void {
  if (disposed) return
  activeDeletionTaskId.value = taskId; activeDeletionKind.value = kind; stopDeletionMonitor(); void pollDeletionTask(); deletionMonitor = setInterval(() => void pollDeletionTask(), 2_000)
}
async function deleteSourceCache(): Promise<void> {
  if (deletionPending.value) return
  const submission = ++deletionSubmission
  deletionSubmitting.value = true
  deletionError.value = null
  try { const taskId = (await requestSourceCacheDeletion(idempotencyKey('source-cache'))).task_id; if (!disposed && submission === deletionSubmission) { sourceDeletionTaskId.value = taskId; monitorDeletion(taskId, 'source-cache') } }
  catch { if (!disposed && submission === deletionSubmission) deletionError.value = '来源缓存删除任务创建失败，请稍后重试。' }
  finally { if (!disposed && submission === deletionSubmission) deletionSubmitting.value = false }
}
async function deleteAllData(): Promise<void> {
  if (allDataConfirmation.value !== 'DELETE ALL DATA') { deletionError.value = '请输入 DELETE ALL DATA 以确认。'; return }
  if (deletionPending.value) return
  const submission = ++deletionSubmission
  deletionSubmitting.value = true
  deletionError.value = null
  try { const taskId = (await requestAllDataDeletion(allDataConfirmation.value, idempotencyKey('all-data'))).task_id; if (!disposed && submission === deletionSubmission) { allDataDeletionTaskId.value = taskId; monitorDeletion(taskId, 'all-data') } }
  catch { if (!disposed && submission === deletionSubmission) deletionError.value = '全部数据删除任务创建失败，请稍后重试。' }
  finally { if (!disposed && submission === deletionSubmission) deletionSubmitting.value = false }
}
onMounted(() => void load())
onUnmounted(() => { disposed = true; stopDeletionMonitor() })
</script>
<template>
  <section>
    <h1>设置</h1>
    <form @submit.prevent="save">
      <label>时区 <select v-model="form.timezone"><option v-for="timezone in timezones" :key="timezone">{{ timezone }}</option></select></label>
      <label>语言 <input v-model="form.locale" /></label>
      <label>简报时间 <input v-model="form.brief_time" type="time" /></label>
      <label>邮件正文保留天数 <input v-model.number="form.email_body_retention_days" type="number" /></label>
      <label>来源元数据保留天数 <input v-model.number="form.source_metadata_retention_days" type="number" /></label>
      <label>工作区历史保留天数 <input v-model.number="form.workspace_history_retention_days" type="number" /></label>
      <button type="submit">保存</button>
    </form>
    <p v-if="saved" role="status">已保存</p>
    <p v-if="error" role="alert">{{ error }}</p>

    <section aria-labelledby="privacy-heading">
      <h2 id="privacy-heading">隐私与数据</h2>
      <button type="button" :disabled="deletionPending" @click="deleteSourceCache">删除来源缓存</button>
      <RouterLink v-if="sourceDeletionTaskId" :to="{ path: '/tasks', query: { task_id: sourceDeletionTaskId } }">查看来源缓存删除任务</RouterLink>
      <label>确认全部删除 <input v-model="allDataConfirmation" autocomplete="off" /></label>
      <button type="button" :disabled="deletionPending" @click="deleteAllData">删除全部数据</button>
      <RouterLink v-if="allDataDeletionTaskId" :to="{ path: '/tasks', query: { task_id: allDataDeletionTaskId } }">查看全部数据删除任务</RouterLink>
      <p v-if="deletionError" role="alert">{{ deletionError }}</p>
    </section>

    <h2>活动会话</h2>
    <ul><li v-for="session in sessions" :key="session.id">{{ session.created_at }} · {{ session.expires_at }} <button type="button" @click="revoke(session.id)">撤销</button></li></ul>
  </section>
</template>

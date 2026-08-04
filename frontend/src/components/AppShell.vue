<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import TaskTimeline from './TaskTimeline.vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useTasksStore } from '@/stores/tasks'
import { getSystemAlerts, type SystemAlert } from '@/api/system'
const route = useRoute(); const tasks = useTasksStore(); const task = computed(() => typeof route.query.task_id === 'string' ? tasks.tasks[route.query.task_id] ?? null : null)
const alerts = ref<SystemAlert[]>([])
const alertsLoading = ref(true)
const alertsError = ref(false)
let alertTimer: ReturnType<typeof setInterval> | null = null
let alertRequest = 0
async function loadAlerts(): Promise<void> { const request = ++alertRequest; alertsLoading.value = true; try { const result = await getSystemAlerts(); if (request !== alertRequest) return; alerts.value = result.alerts; alertsError.value = false } catch { if (request === alertRequest) alertsError.value = true /* 旧轮询失败不得覆盖更新快照。 */ } finally { if (request === alertRequest) alertsLoading.value = false } }
onMounted(() => { void loadAlerts(); alertTimer = setInterval(() => void loadAlerts(), 60_000) })
onUnmounted(() => { if (alertTimer !== null) clearInterval(alertTimer) })
watch(
  () => Object.values(tasks.tasks).map((item) => `${item.id}:${item.status}`).join(','),
  (current, previous) => {
    // 任务进入终态后立即刷新派生告警，避免用户等待下一次固定轮询。
    if (current !== previous && /:(succeeded|failed|cancelled)(,|$)/.test(current)) void loadAlerts()
  },
)
const retry = async (id: string) => { throw new Error(`请在任务页重试 ${id}`) }
</script>
<template><div class="app-shell"><p v-if="alertsLoading" role="status">正在检查系统告警</p><p v-if="alertsError" role="alert">系统告警暂时无法刷新。</p><div v-if="alerts.length" class="overdue-alert" role="alert"><strong>每日简报已逾期</strong><RouterLink v-if="alerts[0]?.diagnostic_task_id" :to="{ path: '/tasks', query: { task_id: alerts[0].diagnostic_task_id } }">查看诊断任务</RouterLink></div><nav aria-label="主导航"><RouterLink to="/chat">新聊天</RouterLink><RouterLink to="/brief">今日简报</RouterLink><RouterLink to="/tasks">任务历史</RouterLink><RouterLink to="/connections">连接</RouterLink><RouterLink to="/settings">设置</RouterLink></nav><main><RouterView /></main><aside><TaskTimeline :task="task" :retry="retry" :follow="(id) => undefined" /></aside></div></template>
<style scoped>.app-shell{display:grid;grid-template-columns:12rem minmax(0,1fr) 20rem;min-height:100vh}.overdue-alert{grid-column:1/-1;background:#a61b1b;color:#fff;padding:.75rem 1rem;display:flex;gap:1rem}.overdue-alert a{color:#fff}nav{display:flex;flex-direction:column;gap:1rem;padding:1rem;border-right:1px solid #ddd}main{padding:1.5rem}@media(max-width:800px){.app-shell{display:block}.overdue-alert{position:sticky;top:0;z-index:2}nav{flex-direction:row;overflow:auto}.app-shell>aside{display:none}}</style>

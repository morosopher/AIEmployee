<script setup lang="ts">
import { RouterView } from 'vue-router'
import AppNavigation from './AppNavigation.vue'
import SystemAlertBanner from './SystemAlertBanner.vue'
import TimelineDrawer from './TimelineDrawer.vue'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'
import TaskTimeline from './TaskTimeline.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useTasksStore } from '@/stores/tasks'
import { getSystemAlerts, type SystemAlert } from '@/api/system'
const route = useRoute()
/** 自有详情页不再挂全局时间线，避免第二个无真实动作回调的重试入口。 */
const hasGlobalTimeline = computed(() => route.path !== '/actions' && route.path !== '/tasks')
const tasks = useTasksStore()
const task = computed(() =>
  typeof route.query.task_id === 'string'
    ? (tasks.tasks[route.query.task_id] ?? null)
    : null,
)
const alerts = ref<SystemAlert[]>([])
const alertsLoading = ref(true)
const alertsError = ref(false)
let alertTimer: ReturnType<typeof setInterval> | null = null
let alertRequest = 0
/** 拉取告警快照；递增请求序号拒绝旧轮询覆盖新响应，失败保留最后可用告警。 */
async function loadAlerts(): Promise<void> {
  const request = ++alertRequest
  alertsLoading.value = true
  try {
    const result = await getSystemAlerts()
    if (request !== alertRequest) return
    alerts.value = result.alerts
    alertsError.value = false
  } catch {
    if (request === alertRequest)
      alertsError.value = true /* 旧轮询失败不得覆盖更新快照。 */
  } finally {
    if (request === alertRequest) alertsLoading.value = false
  }
}
onMounted(() => {
  void loadAlerts()
  alertTimer = setInterval(() => void loadAlerts(), 60_000)
})
onUnmounted(() => {
  if (alertTimer !== null) clearInterval(alertTimer)
})
watch(
  () =>
    Object.values(tasks.tasks)
      .map((item) => `${item.id}:${item.status}`)
      .join(','),
  (current, previous) => {
    // 任务进入终态后立即刷新派生告警，避免用户等待下一次固定轮询。
    if (
      current !== previous &&
      /:(succeeded|failed|cancelled)(,|$)/.test(current)
    )
      void loadAlerts()
  },
)
const retry = async (id: string) => {
  throw new Error(`请在任务页重试 ${id}`)
}

/** 只读消费 useTaskEvents 已维护的连接投影；卸载造成的 disconnected 不视为恢复。 */
const toast = useToast()
const disconnected = computed(() =>
  Object.values(tasks.connections).some((state) => state === 'reconnecting'),
)
watch(
  () => ({ ...tasks.connections }),
  (current, previous) => {
    if (
      Object.entries(current).some(
        ([id, state]) =>
          state === 'connected' && previous[id] === 'reconnecting',
      )
    ) {
      toast.add({ severity: 'success', summary: '任务连接已恢复', life: 4000 })
    }
  },
)
/** 两个互斥抽屉与确认框共用背景隔离；Portal 浮层位于外壳之外。 */
const navigationOpen = ref(false)
const timelineOpen = ref(false)
const confirmationOpen = ref(false)
const mainContent = ref<HTMLElement | null>(null)
let confirmationTrigger: HTMLElement | null = null
/** 确认框显示前保存焦点，Dialog 原生归还焦点时背景可能仍处于更新中的 inert。 */
function showConfirmation(): void {
  confirmationTrigger =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
  confirmationOpen.value = true
}
/**
 * 退出动画结束后解除隔离并归还焦点，留出异步确认结算及按钮更新的时间。
 * 触发器已禁用、移除或无法接收焦点时退回主内容，不把焦点留在已关闭的浮层。
 */
async function hideConfirmation(): Promise<void> {
  confirmationOpen.value = false
  await nextTick()
  const trigger = confirmationTrigger
  confirmationTrigger = null
  if (
    trigger?.isConnected &&
    !trigger.matches(':disabled, [aria-disabled="true"]')
  ) {
    trigger.focus()
    if (document.activeElement === trigger) return
  }
  mainContent.value?.focus()
}
const modalOpen = computed(
  () => navigationOpen.value || timelineOpen.value || confirmationOpen.value,
)
watch(
  () => route.path,
  () => {
    timelineOpen.value = false
  },
)
</script>
<template>
  <div
    :inert="modalOpen || undefined"
    class="grid min-h-screen grid-cols-1 content-start bg-surface-50 text-color md:grid-cols-[14rem_minmax(0,1fr)]"
    :class="{
      'xl:grid-cols-[14rem_minmax(0,1fr)_20rem]': hasGlobalTimeline,
    }"
  >
    <div class="col-span-full">
      <SystemAlertBanner
        :loading="alertsLoading"
        :error="alertsError"
        :alerts="alerts"
      />
      <Message
        v-if="disconnected"
        severity="warn"
        role="status"
        aria-live="polite"
      >
        任务连接已断开，正在尝试恢复。
      </Message>
    </div>
    <AppNavigation @modal-change="navigationOpen = $event" />
    <main
      ref="mainContent"
      tabindex="-1"
      class="min-w-0 p-4 md:p-6"
    >
      <RouterView />
    </main>
    <TimelineDrawer
      v-if="hasGlobalTimeline"
      @modal-change="timelineOpen = $event"
    >
      <TaskTimeline
        :task="task"
        :retry="retry"
        :follow="(id) => undefined"
      />
    </TimelineDrawer>
  </div>
  <!-- 覆盖 Toast 默认 assertive：普通结果礼貌播报，错误仍立即播报。 -->
  <Toast
    :inert="modalOpen || undefined"
    :pt="{
      message: ({ props }) => ({
        role: props.message?.severity === 'error' ? 'alert' : 'status',
        'aria-live':
          props.message?.severity === 'error' ? 'assertive' : 'polite',
      }),
    }"
  />
  <ConfirmDialog
    @show="showConfirmation"
    @after-hide="hideConfirmation"
  />
</template>

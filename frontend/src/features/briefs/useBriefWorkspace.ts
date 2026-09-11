import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  getBrief,
  getTodayBrief,
  listBriefs,
  generateBrief,
} from '@/api/briefs'
import type { Brief } from '@/api/types'
import { useTasksStore } from '@/stores/tasks'
import { useTaskEvents } from '@/composables/useTaskEvents'

/**
 * 维持简报版本和生成任务投影；选择历史版本不因后台生成而跳回最新。
 * @returns 只读版本导航及显式生成；迟到响应和卸载后的读取被代际隔离。
 */
export function useBriefWorkspace() {
  const latest = ref<Brief | null>(null),
    selected = ref<Brief | null>(null),
    versions = ref<Brief[]>([])
  const loading = ref(true),
    generating = ref(false),
    error = ref<string | null>(null),
    taskId = ref<string | null>(null)
  const tasks = useTasksStore()
  let epoch = 0,
    selectionEpoch = 0,
    disposed = false
  const task = computed(() =>
    taskId.value ? tasks.tasks[taskId.value] : undefined,
  )
  useTaskEvents(taskId, (event) => {
    if (event.event === 'brief.ready') void load()
  })
  watch(
    () => task.value?.status,
    (status) => {
      if (status === 'succeeded') void load()
    },
  )
  onUnmounted(() => {
    disposed = true
    epoch += 1
    selectionEpoch += 1
  })

  /** 保留用户主动选择的历史版本；两个顺序读取形成完整页面状态后才发布。 */
  async function load(): Promise<void> {
    const owner = ++epoch
    loading.value = true
    error.value = null
    try {
      const value = await getTodayBrief(),
        history = await listBriefs(value.local_date)
      if (disposed || owner !== epoch) return
      const keepHistorical =
        selected.value !== null && selected.value.id !== latest.value?.id
      latest.value = value
      versions.value = history
      if (!keepHistorical) selected.value = value
    } catch {
      if (!disposed && owner === epoch) error.value = '暂无今日简报或加载失败。'
    } finally {
      if (!disposed && owner === epoch) loading.value = false
    }
  }
  /** @param id 用户选择的真实简报版本。 @returns 权威历史内容，失败不替换当前版本。 */
  async function select(id: string): Promise<void> {
    const owner = ++selectionEpoch
    try {
      const value = id === latest.value?.id ? latest.value : await getBrief(id)
      if (!disposed && owner === selectionEpoch) selected.value = value
    } catch {
      if (!disposed && owner === selectionEpoch)
        error.value = '无法加载历史版本。'
    }
  }
  /** 生成只接受排队事实，实际简报通过原 SSE 与 REST 恢复。 */
  async function refresh(): Promise<void> {
    if (generating.value) return
    generating.value = true
    try {
      const result = await generateBrief()
      if (!disposed) taskId.value = result.task_id
    } catch {
      if (!disposed) error.value = '生成任务失败，请重试。'
    } finally {
      if (!disposed) generating.value = false
    }
  }
  onMounted(() => {
    void load()
  })
  return {
    latest,
    selected,
    versions,
    loading,
    generating,
    error,
    load,
    select,
    refresh,
  }
}

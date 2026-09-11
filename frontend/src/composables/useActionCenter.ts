import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ActionItemKind, ActionProvider, ActionStatus } from '@/api/types'
import { groupActions } from '@/features/actions/presentation'
import { useActionsStore } from '@/stores/actions'
import { useAuthStore } from '@/stores/auth'
import { useTaskEvents } from './useTaskEvents'

/**
 * 组合操作中心页面的 REST、筛选与唯一任务流生命周期，页面只负责语义化展示。
 * @returns 仅内存的页面投影与选择/刷新操作；卸载会移除监听器并使在途响应失效。
 */
export function useActionCenter() {
  const actions = useActionsStore()
  const auth = useAuthStore()
  const provider = ref<ActionProvider | ''>('')
  const itemKind = ref<ActionItemKind | ''>('')
  const status = ref<ActionStatus | ''>('')
  const selectedTaskId = ref<string | null>(null)
  const timezone = computed(() => auth.user?.timezone ?? 'UTC')
  const selected = computed(() =>
    selectedTaskId.value
      ? (actions.snapshots[selectedTaskId.value] ?? null)
      : null,
  )
  const groups = computed(() => groupActions(actions.items))
  const connectionState = useTaskEvents(
    selectedTaskId,
    (event) => actions.applyEvent(event),
    () => {
      void refresh()
    },
  )

  /** 焦点/重连只重取 REST；无 task_id 的本地编辑对象也由列表重新发现。 */
  async function refresh(): Promise<void> {
    await Promise.allSettled([
      actions.refreshList(),
      selectedTaskId.value
        ? actions.loadSnapshot(selectedTaskId.value)
        : Promise.resolve(),
    ])
  }
  /**
   * @param taskId 真实列表任务 ID。
   * @returns 无返回值，本地对象不会调用此入口。
   */
  function selectTask(taskId: string): void {
    selectedTaskId.value = taskId
    void actions.loadSnapshot(taskId)
  }
  /** 关闭详情将触发既有 task stream 的关闭，不改写任何业务状态。 */
  function closeDetail(): void {
    selectedTaskId.value = null
  }
  /**
   * @param nextOffset 服务端分页偏移。
   * @returns 当前筛选的新页面。
   */
  async function changePage(nextOffset: number): Promise<void> {
    closeDetail()
    await actions.refreshList({ ...actions.filters, offset: nextOffset })
  }
  const onFocus = (): void => {
    void refresh()
  }
  watch([provider, itemKind, status], () => {
    closeDetail()
    void actions.refreshList({
      ...(provider.value ? { provider: provider.value } : {}),
      ...(itemKind.value ? { item_kind: itemKind.value } : {}),
      ...(status.value ? { status: status.value } : {}),
    })
  })
  onMounted(() => {
    void refresh()
    window.addEventListener('focus', onFocus)
    window.addEventListener('online', onFocus)
  })
  onUnmounted(() => {
    window.removeEventListener('focus', onFocus)
    window.removeEventListener('online', onFocus)
    actions.clear()
  })
  return {
    actions,
    provider,
    itemKind,
    status,
    selectedTaskId,
    selected,
    timezone,
    groups,
    connectionState,
    refresh,
    selectTask,
    closeDetail,
    changePage,
  }
}

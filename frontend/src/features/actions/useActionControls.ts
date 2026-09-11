import { onUnmounted, ref, type Ref } from 'vue'
import { decideApproval } from '@/api/approvals'
import { reconcileAction, resolveAction } from '@/api/actions'
import { cancelTask } from '@/api/client'
import type { ActionSnapshot, ManualResolution } from '@/api/types'
import { useActionsStore } from '@/stores/actions'
import { actionRecovery, type ActionRecovery } from './recovery'

/**
 * @param snapshot 当前已验证的操作快照；人工确认直接使用其字符串版本。
 * @param changed 重取关联编辑对象的通知，不授予任何供应商写权限。
 * @returns 精确审批、撤回、只读核对和人工结论入口；完成后只重读服务端事实。
 */
export function useActionControls(
  snapshot: Ref<ActionSnapshot>,
  changed: () => void,
) {
  const actions = useActionsStore()
  const busy = ref(false),
    error = ref<ActionRecovery | null>(null)
  let disposed = false
  onUnmounted(() => {
    disposed = true
  })

  /** 回执后的权威读取不能用最小响应替代；旧任务回调不得通知新详情。 */
  async function refresh(
    taskId = snapshot.value.task_id,
    minimumCursor?: string,
  ): Promise<void> {
    await actions.loadSnapshot(taskId, minimumCursor)
    await actions.refreshList()
    if (!disposed && snapshot.value.task_id === taskId) changed()
  }
  /** 同一详情的敏感按钮共享互斥边界；服务端继续防御其他标签页竞争。 */
  async function perform(operation: () => Promise<void>): Promise<void> {
    if (busy.value) throw new Error('Action already pending')
    busy.value = true
    try {
      await operation()
    } finally {
      if (!disposed) busy.value = false
    }
  }
  async function decide(
    id: string,
    decision: 'approved' | 'rejected',
    version: number,
    hash: string,
  ): Promise<void> {
    const taskId = snapshot.value.task_id
    await perform(async () => {
      await decideApproval(id, decision, version, hash)
      await refresh(taskId)
    })
  }
  async function reconcile(taskId: string): Promise<void> {
    await perform(async () => {
      await reconcileAction(taskId)
      await refresh(taskId)
    })
  }
  async function resolve(
    taskId: string,
    resolution: ManualResolution,
    taskVersion: string,
  ): Promise<void> {
    await perform(async () => {
      const receipt = await resolveAction(taskId, resolution, taskVersion)
      await refresh(taskId, receipt.task_version)
    })
  }
  /** 撤回只调用既有 Task 取消入口，等待它释放冻结对象；不能以编辑 PATCH 绕过审批。 */
  async function withdraw(): Promise<void> {
    const taskId = snapshot.value.task_id
    error.value = null
    try {
      await perform(async () => {
        await cancelTask(taskId)
        await refresh(taskId)
      })
    } catch (cause) {
      if (!disposed && snapshot.value.task_id === taskId)
        error.value = actionRecovery(cause)
    }
  }
  return { busy, error, refresh, decide, reconcile, resolve, withdraw }
}

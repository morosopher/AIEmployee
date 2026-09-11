import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { createRestoreProposal } from '@/api/calendar'
import { createRequestIntent, type RequestIntent } from '@/api/requestIntent'
import type { CalendarProposal } from '@/api/types'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'

/**
 * @param proposal 服务端已验证的当前提案，只有精确可用的恢复来源才能创建准备任务。
 * @param locked 页面已有请求互斥状态，防止刷新来源时提交过时身份。
 * @param followTask 收到真实202后进入现有任务页，任务进度和结果仍由GET/SSE恢复。
 * @returns 显式准备动作、互斥状态和稳定错误；运输失败不更换意图、不自动重放。
 */
export function useCalendarRestore(
  proposal: Ref<CalendarProposal | null>,
  locked: Ref<boolean>,
  followTask: (taskId: string) => Promise<unknown>,
) {
  const source = computed(() =>
    proposal.value?.operation_kind === 'update' &&
    proposal.value.status === 'applied'
      ? (proposal.value.editor_facts?.restore_source ?? null)
      : null,
  )
  const busy = ref(false),
    error = ref<ActionRecovery | null>(null)
  let intent: RequestIntent | null = null,
    epoch = 0,
    disposed = false
  watch(
    () =>
      `${proposal.value?.id}:${source.value?.event_id}:${source.value?.snapshot_id}`,
    () => {
      // 同一来源重新GET不丢失未知结果的创建键；切换对象后旧回执不得导航新页面。
      epoch += 1
      busy.value = false
      error.value = null
      intent = null
    },
  )
  onUnmounted(() => {
    disposed = true
    epoch += 1
  })

  /** 只排队恢复准备；这里不会修改原提案、提交审批或调用供应商写入。 */
  async function prepare(): Promise<void> {
    const selected = source.value
    if (!selected || busy.value || locked.value) return
    const owner = epoch
    intent ??= createRequestIntent()
    busy.value = true
    error.value = null
    try {
      const receipt = await createRestoreProposal(
        selected.event_id,
        selected.snapshot_id,
        intent,
      )
      if (!disposed && owner === epoch) await followTask(receipt.task_id)
    } catch (cause) {
      if (!disposed && owner === epoch) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === epoch) busy.value = false
    }
  }
  return { source, busy, error, prepare }
}

<script setup lang="ts">
import { inject, nextTick, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { actionDialogContext } from './actionDialogContext'

/** 对话框仅收集显式点击；调用者仍负责枚举提交、忙碌状态和权威快照刷新。 */
const props = defineProps<{ title: string; busy: boolean }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
const visible = ref(true)
const openDialog = inject(actionDialogContext, null)
const previousFocus =
  document.activeElement instanceof HTMLElement ? document.activeElement : null
let release: (() => Promise<void>) | undefined
const cancelled = ref(false)
let disposed = false

/** Portal 已显示才隔离背景；AppShell 不存在时仍支持独立组件的有效触发器返回。 */
function shown(): void {
  if (disposed) return
  release = openDialog?.(previousFocus)
}
/**
 * 取消先锁住按钮，再退出浮层并发出原 cancel，避免父级 v-if 立即卸载跳过退出动画。
 * Dialog 的离场节点保留关闭前 VNode，因此等待一次 DOM 更新再隐藏，防止退出期按钮重新可用。
 * Escape 始终由 Dialog 监听，回调在 busy 时拒绝关闭；初始 busy 后恢复也可正常 Escape。
 */
async function requestCancel(): Promise<void> {
  if (props.busy || cancelled.value || disposed) return
  cancelled.value = true
  await nextTick()
  if (!disposed) visible.value = false
}
/** 隔离释放幂等；强制卸载与正常 after-hide 可能同时触发，不重复归还焦点。 */
async function restoreFocus(): Promise<void> {
  if (release) {
    const close = release
    release = undefined
    await close()
    return
  }
  if (openDialog) return
  await nextTick()
  if (
    previousFocus?.isConnected &&
    !previousFocus.matches(':disabled, [aria-disabled="true"]') &&
    !previousFocus.closest('[inert]')
  )
    previousFocus.focus()
}
/** 正常关闭动画后才通知父组件；确认不关闭，等待调用者刷新权威结果后卸载。 */
function hidden(): void {
  void restoreFocus()
  if (cancelled.value && !disposed) emit('cancel')
}
onUnmounted(() => {
  disposed = true
  void restoreFocus()
})
</script>

<template>
  <Dialog
    :visible="visible"
    :header="title"
    modal
    :closable="false"
    :draggable="false"
    :close-on-escape="true"
    class="m-4 w-full max-w-lg"
    @update:visible="requestCancel"
    @show="shown"
    @after-hide="hidden"
  >
    <slot />
    <template #footer>
      <!-- 退出动画保留节点期间也锁住原按钮，防止取消后的迟到点击变成确认。 -->
      <div class="flex flex-wrap gap-3">
        <Button
          type="button"
          name="cancel-resolution"
          label="取消"
          severity="secondary"
          autofocus
          :disabled="busy || cancelled"
          @click="requestCancel"
        />
        <Button
          type="button"
          name="confirm-resolution"
          label="确认记录结果"
          :disabled="busy || cancelled"
          :loading="busy"
          :aria-busy="busy"
          @click="!busy && !cancelled && !disposed && emit('confirm')"
        />
      </div>
    </template>
  </Dialog>
</template>

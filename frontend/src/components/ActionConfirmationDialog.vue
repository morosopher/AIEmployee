<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'

/** 对话框仅收集显式点击；调用者负责枚举提交及随后权威状态刷新。 */
defineProps<{ title: string; busy: boolean }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
const region = ref<HTMLElement | null>(null)
const previousFocus =
  document.activeElement instanceof HTMLElement ? document.activeElement : null
onMounted(
  () =>
    void nextTick(() =>
      region.value
        ?.querySelector<HTMLButtonElement>('button[name="cancel-resolution"]')
        ?.focus(),
    ),
)
onUnmounted(() => previousFocus?.focus())

/** @param event 对话框键盘事件。保持焦点循环，防止触发背景动作。 */
function containFocus(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return
  const buttons = Array.from(
    region.value?.querySelectorAll<HTMLButtonElement>(
      'button:not(:disabled)',
    ) ?? [],
  )
  const first = buttons[0],
    last = buttons[buttons.length - 1]
  if (!first || !last) {
    event.preventDefault()
    return
  }
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>
<template>
  <div class="dialog-backdrop">
    <section
      ref="region"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      class="confirmation-dialog"
      @keydown="containFocus"
      @keydown.esc.prevent="!busy && emit('cancel')"
    >
      <h3>{{ title }}</h3>
      <slot />
      <div class="controls">
        <button
          type="button"
          name="cancel-resolution"
          :disabled="busy"
          @click="emit('cancel')"
        >
          取消
        </button><button
          type="button"
          name="confirm-resolution"
          :disabled="busy"
          @click="emit('confirm')"
        >
          确认记录结果
        </button>
      </div>
    </section>
  </div>
</template>
<style scoped>
.dialog-backdrop {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: grid;
  place-items: center;
  background: #0f172a80;
  padding: 1rem;
}
.confirmation-dialog {
  background: white;
  padding: 1.25rem;
  border-radius: 0.75rem;
  max-width: 32rem;
  max-height: 90vh;
  overflow: auto;
}
.controls {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}
button:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

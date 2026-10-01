<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { breakpointsTailwind, useBreakpoints } from '@vueuse/core'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import Panel from 'primevue/panel'

/** 插槽沿用原 TaskTimeline；这里只按断点切换容器，不创建任务订阅。 */
const emit = defineEmits<{ modalChange: [open: boolean] }>()
defineSlots<{ default(): unknown }>()
const breakpoints = useBreakpoints(breakpointsTailwind)
const narrow = breakpoints.smaller('md')
const wide = breakpoints.greaterOrEqual('xl')
const visible = ref(false)
let trigger: HTMLElement | null = null

/** 保存触发元素并通知外壳隔离背景，任务数据保持来自调用方。 */
function open(event: Event): void {
  trigger =
    event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  visible.value = true
  emit('modalChange', true)
}

/** PrimeVue Drawer 关闭过渡结束后，先解除 inert，再恢复焦点。 */
async function afterHide(): Promise<void> {
  emit('modalChange', false)
  await nextTick()
  if (trigger?.isConnected) trigger.focus()
}
watch(narrow, () => {
  visible.value = false
  void afterHide()
})
</script>

<template>
  <aside
    v-if="wide"
    aria-label="任务时间线"
    class="min-w-0 border-l border-surface bg-surface-0"
  >
    <slot />
  </aside>
  <Panel
    v-else-if="!narrow"
    header="任务时间线"
    toggleable
    collapsed
    class="min-w-0 md:col-start-2"
    :toggle-button-props="{ 'aria-label': '任务时间线' }"
  >
    <slot />
  </Panel>
  <div
    v-else
    class="p-4"
  >
    <Button
      label="任务时间线"
      icon="pi pi-history"
      aria-label="打开任务时间线"
      aria-controls="task-timeline-drawer"
      :aria-expanded="visible"
      @click="open"
    />
    <Drawer
      id="task-timeline-drawer"
      v-model:visible="visible"
      header="任务时间线"
      aria-label="任务时间线"
      role="dialog"
      position="right"
      modal
      block-scroll
      @after-hide="afterHide"
    >
      <slot />
    </Drawer>
  </div>
</template>

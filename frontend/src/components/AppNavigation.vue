<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { breakpointsTailwind, useBreakpoints } from '@vueuse/core'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import Menu from 'primevue/menu'

/** 主导航仅承担路由呈现和移动端模态生命周期，不参与业务状态。 */
const emit = defineEmits<{ modalChange: [open: boolean] }>()
const route = useRoute()
const narrow = useBreakpoints(breakpointsTailwind).smaller('md')
const visible = ref(false)
let trigger: HTMLElement | null = null
const items = [
  { label: '新聊天', route: '/chat' },
  { label: '今日简报', route: '/brief' },
  { label: '操作中心', route: '/actions' },
  { label: '任务历史', route: '/tasks' },
  { label: '连接', route: '/connections' },
  { label: '设置', route: '/settings' },
]

/** 记录真实触发按钮；由外壳同步隔离背景，Drawer 自身负责焦点陷阱。 */
function open(event: Event): void {
  trigger =
    event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  visible.value = true
  emit('modalChange', true)
}

/** 等待背景解除 inert 后归还焦点，避免浏览器拒绝聚焦不可交互元素。 */
async function afterHide(): Promise<void> {
  emit('modalChange', false)
  await nextTick()
  if (trigger?.isConnected) trigger.focus()
}

// 路由选择关闭抽屉；跨断点会卸载 Drawer，必须同步解除外壳的模态隔离。
watch(
  () => route.fullPath,
  () => {
    visible.value = false
  },
)
watch(narrow, () => {
  visible.value = false
  void afterHide()
})
</script>

<template>
  <div class="p-4">
    <Button
      v-if="narrow"
      icon="pi pi-bars"
      aria-label="打开导航"
      aria-controls="primary-navigation-drawer"
      :aria-expanded="visible"
      @click="open"
    />
    <component
      :is="narrow ? Drawer : 'div'"
      id="primary-navigation-drawer"
      v-model:visible="visible"
      :modal="narrow"
      :block-scroll="narrow"
      :role="narrow ? 'dialog' : undefined"
      :aria-label="narrow ? '主导航' : undefined"
      header="主导航"
      @after-hide="afterHide"
    >
      <nav aria-label="主导航">
        <Menu
          :model="items"
          class="w-full border-0"
        >
          <template #item="{ item, props }">
            <RouterLink
              v-bind="props.action"
              :to="item.route"
              role="link"
              :aria-current="route.path === item.route ? 'page' : undefined"
              class="rounded text-color focus-visible:outline focus-visible:outline-primary"
              :class="{
                'bg-highlight font-semibold': route.path === item.route,
              }"
            >
              {{ item.label }}
            </RouterLink>
          </template>
        </Menu>
      </nav>
    </component>
  </div>
</template>

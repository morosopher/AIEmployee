<script setup lang="ts">
import { defineAsyncComponent, defineComponent, h, ref } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'

/** 页面只装载展示工作区；所有列表/快照/SSE hooks 在唯一工作区实例内运行。 */
const moduleFailed = ref(false)
const ActionCenterWorkspace = defineAsyncComponent({
  loader: () => import('@/components/ActionCenterWorkspace.vue'),
  delay: 0,
  loadingComponent: defineComponent({
    setup: () => () =>
      moduleFailed.value
        ? null
        : h('div', { class: 'space-y-3' }, [
            h(
              Message,
              { severity: 'secondary', role: 'status', 'aria-live': 'polite' },
              () => '正在加载操作中心…',
            ),
            h(Skeleton, { height: '6rem' }),
          ]),
  }),
  onError() {
    moduleFailed.value = true
  },
})
/** 浏览器会缓存失败的模块导入；明确整页重载重试，不声称重复 import 能恢复。 */
function retryWorkspace(): void {
  window.location.reload()
}
/** 新增模块加载 status / 加载失败 alert；互斥呈现，不重复播报旧工作区的业务状态。 */
</script>

<template>
  <ActionCenterWorkspace />
  <Message
    v-if="moduleFailed"
    severity="error"
    role="alert"
  >
    操作中心加载失败，请重试。重试会重新加载页面。
    <Button
      label="重试加载操作中心"
      severity="secondary"
      @click="retryWorkspace"
    />
  </Message>
</template>

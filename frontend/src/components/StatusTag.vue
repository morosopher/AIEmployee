<script setup lang="ts">
import { computed } from 'vue'
import Tag from 'primevue/tag'
import { statusPresentation, type StatusValueByKind } from '@/design/status'

/** 状态族与值保持判别联合；不能把能力撤销等状态作为任务状态传入。 */
type Props =
  | { kind: 'task'; value: StatusValueByKind['task'] }
  | { kind: 'action'; value: StatusValueByKind['action'] }
  | { kind: 'capability'; value: StatusValueByKind['capability'] }
  | { kind: 'approval'; value: StatusValueByKind['approval'] }
  | { kind: 'connection'; value: StatusValueByKind['connection'] }
const props = defineProps<Props>()

/** 按判别字段收窄后调用唯一设计映射，响应服务端状态更新，不自行迁移状态。 */
const presentation = computed(() => {
  switch (props.kind) {
    case 'task':
      return statusPresentation('task', props.value)
    case 'action':
      return statusPresentation('action', props.value)
    case 'capability':
      return statusPresentation('capability', props.value)
    case 'approval':
      return statusPresentation('approval', props.value)
  }
  return statusPresentation('connection', props.value)
})
</script>

<template>
  <Tag
    :severity="presentation.severity"
    :value="presentation.label"
  />
</template>

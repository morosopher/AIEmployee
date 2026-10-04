<script setup lang="ts">
import Button from 'primevue/button'
import Message from 'primevue/message'
import Panel from 'primevue/panel'
import { computed, watch } from 'vue'
import { RouterLink, type LocationQueryValue } from 'vue-router'
import { useActionsStore } from '@/stores/actions'
import { useAuthStore } from '@/stores/auth'
import { useTaskEvents } from '@/composables/useTaskEvents'
import ActionDetail from './ActionDetail.vue'

/** task query 只携带 UUID；必须重读并核对 local_action 归属后才提供任何审批控制。 */
const props = defineProps<{
  localId: string
  itemKind: 'mail_draft' | 'calendar_proposal'
  taskQuery: LocationQueryValue | LocationQueryValue[] | undefined
}>()
defineEmits<{ changed: [] }>()
const actions = useActionsStore(),
  auth = useAuthStore()
const taskId = computed(() =>
  typeof props.taskQuery === 'string' &&
  /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(props.taskQuery)
    ? props.taskQuery
    : null,
)
const snapshot = computed(() =>
  taskId.value ? actions.snapshots[taskId.value] : undefined,
)
const matching = computed(() =>
  snapshot.value?.local_action?.id === props.localId &&
  snapshot.value.local_action.item_kind === props.itemKind
    ? snapshot.value
    : null,
)
const connection = useTaskEvents(
  taskId,
  (event) => actions.applyEvent(event),
  () => {
    if (taskId.value) void actions.loadSnapshot(taskId.value)
  },
)
watch(
  taskId,
  (id) => {
    if (id) void actions.loadSnapshot(id)
  },
  { immediate: true },
)
/** 原关联加载/连接恢复两个 status 与读取失败/归属不匹配两个 alert 原文保留，Panel 不增 live region。 */
</script>
<template>
  <section
    v-if="taskId"
    aria-label="关联审批任务"
  >
    <Panel header="关联审批任务">
      <div class="space-y-4">
        <Message
          v-if="actions.snapshotLoading[taskId]"
          severity="secondary"
          role="status"
          aria-live="polite"
        >
          正在读取关联任务…
        </Message>
        <Message
          v-if="actions.snapshotErrors[taskId]"
          severity="error"
          role="alert"
        >
          关联任务读取失败，请到操作中心重新加载。<span
            v-if="actions.snapshotErrors[taskId]?.trace_id"
          >
            追踪编号：{{ actions.snapshotErrors[taskId]?.trace_id }}</span>
        </Message>
        <Message
          v-if="snapshot && !matching"
          severity="error"
          role="alert"
        >
          此任务与当前编辑对象不匹配，请在操作中心核对。
        </Message>
        <template v-if="matching">
          <Message
            v-if="connection !== 'connected'"
            severity="secondary"
            role="status"
            aria-live="polite"
          >
            任务连接正在恢复，状态以服务端为准。
          </Message>
          <ActionDetail
            :snapshot="matching"
            :timezone="auth.user?.timezone ?? 'UTC'"
            @changed="$emit('changed')"
          />
        </template>
      </div>
    </Panel>
  </section>
  <Button
    v-else
    :as="RouterLink"
    to="/actions"
    link
    label="在操作中心查看与撤回审批"
  />
</template>

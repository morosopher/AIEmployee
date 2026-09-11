<script setup lang="ts">
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
</script>
<template>
  <section
    v-if="taskId"
    aria-label="关联审批任务"
  >
    <p
      v-if="actions.snapshotLoading[taskId]"
      role="status"
    >
      正在读取关联任务…
    </p>
    <p
      v-if="actions.snapshotErrors[taskId]"
      role="alert"
    >
      关联任务读取失败，请到操作中心重新加载。<span
        v-if="actions.snapshotErrors[taskId]?.trace_id"
      >
        追踪编号：{{ actions.snapshotErrors[taskId]?.trace_id }}</span>
    </p>
    <p
      v-if="snapshot && !matching"
      role="alert"
    >
      此任务与当前编辑对象不匹配，请在操作中心核对。
    </p>
    <template v-if="matching">
      <p
        v-if="connection !== 'connected'"
        role="status"
      >
        任务连接正在恢复，状态以服务端为准。
      </p>
      <ActionDetail
        :snapshot="matching"
        :timezone="auth.user?.timezone ?? 'UTC'"
        @changed="$emit('changed')"
      />
    </template>
  </section>
  <RouterLink
    v-else
    to="/actions"
  >
    在操作中心查看与撤回审批
  </RouterLink>
</template>

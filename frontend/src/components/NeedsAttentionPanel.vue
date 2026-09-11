<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import type { ActionSnapshot, ManualResolution } from '@/api/types'
import { providerLabel, safeProviderUrl } from '@/features/actions/presentation'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'
import ActionConfirmationDialog from './ActionConfirmationDialog.vue'

/** 人工结果使用提交时最新的服务端字符串游标；回调收到最小响应后必须重取快照。 */
interface Props {
  snapshot: ActionSnapshot
  reconcile: (taskId: string) => Promise<void>
  resolve: (
    taskId: string,
    resolution: ManualResolution,
    taskVersion: string,
  ) => Promise<void>
  reload?: () => void | Promise<void>
  locked?: boolean
}
const props = defineProps<Props>()
const choice = ref<ManualResolution | null>(null),
  busy = ref(false)
const error = ref<ActionRecovery | null>(null)
const providerUrl = computed(() =>
  safeProviderUrl(props.snapshot.provider_url, props.snapshot.provider),
)
const disabled = computed(
  () =>
    props.locked || busy.value || props.snapshot.status !== 'needs_attention',
)
let generation = 0
watch(
  () => props.snapshot.task_id,
  () => {
    generation += 1
    choice.value = null
    busy.value = false
    error.value = null
  },
)
watch(
  () => props.snapshot.status,
  (status) => {
    if (status !== 'needs_attention') choice.value = null
  },
)
onUnmounted(() => {
  generation += 1
})

/**
 * @param resolution 已明确确认的枚举；空值只发起只读核对。
 * @returns 等待回调刷新权威状态；组件不推测成功或自动重发。
 */
async function act(resolution: ManualResolution | null): Promise<void> {
  if (disabled.value) return
  const owner = generation
  busy.value = true
  error.value = null
  try {
    if (resolution)
      await props.resolve(
        props.snapshot.task_id,
        resolution,
        props.snapshot.task_version,
      )
    else await props.reconcile(props.snapshot.task_id)
    if (owner === generation) choice.value = null
  } catch (cause) {
    if (owner === generation) error.value = actionRecovery(cause)
  } finally {
    if (owner === generation) busy.value = false
  }
}
</script>
<template>
  <section
    class="needs-attention"
    aria-label="人工结果确认"
  >
    <h3>先核实供应商中的实际结果</h3>
    <p>
      核对尝试：{{ snapshot.reconciliation_attempt_count }} · 最后错误：{{
        snapshot.execution?.error_code || snapshot.error_code || '无'
      }}
    </p>
    <a
      v-if="providerUrl"
      :href="providerUrl"
      target="_blank"
      rel="noopener noreferrer"
    >在 {{ providerLabel(snapshot.provider) }} 中检查结果</a>
    <p>重新核对只读取供应商结果。确认未执行不会自动重发。</p>
    <div class="controls">
      <button
        type="button"
        name="reconcile"
        :disabled="disabled"
        @click="act(null)"
      >
        重新核对
      </button><button
        type="button"
        name="confirmed_executed"
        :disabled="disabled"
        @click="choice = 'confirmed_executed'"
      >
        确认已执行
      </button><button
        type="button"
        name="confirmed_not_executed"
        :disabled="disabled"
        @click="choice = 'confirmed_not_executed'"
      >
        确认未执行
      </button>
    </div>
    <p
      v-if="busy"
      role="status"
    >
      正在记录核对请求…
    </p>
    <div
      v-if="error"
      role="alert"
      class="error"
    >
      <p>
        {{ error.message
        }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
      </p>
      <button
        v-if="reload"
        type="button"
        @click="reload"
      >
        重新加载
      </button>
    </div>
    <ActionConfirmationDialog
      v-if="choice"
      :title="choice === 'confirmed_executed' ? '确认已执行' : '确认未执行'"
      :busy="busy"
      @cancel="choice = null"
      @confirm="act(choice)"
    >
      <p role="alert">
        此决定只记录人工结果，不会调用供应商写接口。确认未执行不会自动重发；如需再次执行，必须创建新草稿或提案并重新审批。
      </p>
    </ActionConfirmationDialog>
  </section>
</template>
<style scoped>
.needs-attention {
  background: #fff5df;
  border-left: 3px solid #946200;
  padding: 1rem;
  margin-top: 1rem;
  overflow-wrap: anywhere;
}
.controls {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.error {
  color: #a61b1b;
}
button:focus-visible,
a:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>

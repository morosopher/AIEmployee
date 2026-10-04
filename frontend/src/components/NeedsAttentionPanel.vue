<script setup lang="ts">
import { computed, onUnmounted, ref, useId, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Panel from 'primevue/panel'
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
/** 每个面板独立关联原警告，使键盘聚焦按钮时可读到只读核对／不自动重发的边界。 */
const warningId = useId()
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
  <Panel
    role="region"
    class="mt-4 min-w-0 break-words"
    aria-label="人工结果确认"
  >
    <template #header="{ id }">
      <!-- 接回 Panel 公开标题 ID，保留内置内容区域的可访问名称。 -->
      <h3
        :id="id"
        class="m-0 text-base font-semibold"
      >
        先核实供应商中的实际结果
      </h3>
    </template>
    <div class="space-y-4">
      <!-- 核对事实不是新失败事件；覆盖 Message 默认 assertive，避免每次快照刷新打断用户。 -->
      <Message
        severity="error"
        role="note"
        aria-live="off"
      >
        核对尝试：{{ snapshot.reconciliation_attempt_count }} · 最后错误：{{
          snapshot.execution?.error_code || snapshot.error_code || '无'
        }}
      </Message>
      <Button
        v-if="providerUrl"
        as="a"
        variant="link"
        :href="providerUrl"
        target="_blank"
        rel="noopener noreferrer"
      >
        在 {{ providerLabel(snapshot.provider) }} 中检查结果
      </Button>
      <div class="space-y-3">
        <p
          :id="warningId"
          class="text-sm"
        >
          重新核对只读取供应商结果。确认未执行不会自动重发。
        </p>
        <div class="flex flex-wrap gap-3">
          <Button
            type="button"
            name="reconcile"
            severity="secondary"
            :aria-describedby="warningId"
            :loading="busy && !choice"
            :aria-busy="busy && !choice"
            :disabled="disabled"
            @click="act(null)"
          >
            重新核对
          </Button>
          <Button
            type="button"
            name="confirmed_executed"
            :aria-describedby="warningId"
            :loading="busy && choice === 'confirmed_executed'"
            :aria-busy="busy && choice === 'confirmed_executed'"
            :disabled="disabled"
            @click="choice = 'confirmed_executed'"
          >
            确认已执行
          </Button>
          <Button
            type="button"
            name="confirmed_not_executed"
            severity="secondary"
            :aria-describedby="warningId"
            :loading="busy && choice === 'confirmed_not_executed'"
            :aria-busy="busy && choice === 'confirmed_not_executed'"
            :disabled="disabled"
            @click="choice = 'confirmed_not_executed'"
          >
            确认未执行
          </Button>
        </div>
      </div>
      <!-- choice 存在时背景被 AppShell inert；同一状态只在当前可感知位置呈现一份。 -->
      <Message
        v-if="busy && !choice"
        severity="info"
        role="status"
        aria-live="polite"
      >
        正在记录核对请求…
      </Message>
      <Message
        v-if="error && !choice"
        severity="error"
        role="alert"
      >
        <p>
          {{ error.message
          }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
        </p>
        <Button
          v-if="reload"
          type="button"
          severity="secondary"
          @click="reload"
        >
          重新加载
        </Button>
      </Message>
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
      <!-- 原 act 仍控制 busy/error；这里只投影提示，取消后由上面的互斥分支保留恢复入口。 -->
      <Message
        v-if="busy"
        severity="info"
        role="status"
        aria-live="polite"
        class="mt-4"
      >
        正在记录核对请求…
      </Message>
      <Message
        v-if="error"
        severity="error"
        role="alert"
        class="mt-4"
      >
        <p>
          {{ error.message
          }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
        </p>
        <Button
          v-if="reload"
          type="button"
          severity="secondary"
          @click="reload"
        >
          重新加载
        </Button>
      </Message>
    </ActionConfirmationDialog>
  </Panel>
</template>

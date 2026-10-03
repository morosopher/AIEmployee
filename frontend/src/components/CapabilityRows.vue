<script setup lang="ts">
import { ref, watch } from 'vue'
import DataTable, { type DataTableContext } from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ToggleSwitch from 'primevue/toggleswitch'
import StatusTag from '@/components/StatusTag.vue'
import type {
  CapabilityName,
  ConnectionCapabilities,
  ConnectionCapability,
} from '@/api/types'

/** 能力行仅呈现已校验的服务端状态；关闭依赖及授权结果仍由原动作／服务端裁决。 */
const props = withDefaults(
  defineProps<{
    capabilities: ConnectionCapabilities
    busy: boolean
    disconnected: boolean
    timezone?: string
  }>(),
  { timezone: 'UTC' },
)
const emit = defineEmits<{
  enable: [capability: CapabilityName]
  disable: [capability: CapabilityName]
}>()

/** 仅记录关闭控件的在途意图，不写回能力状态；请求落定后必须重新服从服务端事实。 */
const pendingClosed = ref<Partial<Record<CapabilityName, boolean>>>({})
watch([() => props.busy, () => props.capabilities], () => {
  if (!props.busy) pendingClosed.value = {}
})

/** @param capability 读取能力名称。@returns 需先关闭的写能力，或空值；沿用原依赖规则。 */
function blockedDependency(capability: CapabilityName): CapabilityName | null {
  const write =
    capability === 'mail.read'
      ? 'mail.send'
      : capability === 'calendar.read'
        ? 'calendar.write'
        : null
  return write &&
    props.capabilities.capabilities.some(
      (row) =>
        row.capability === write &&
        !['disabled', 'revoked'].includes(row.status),
    )
    ? write
    : null
}

/** 关闭开关只表示本地 disabled，不能把授权中、降级或撤销误报成已获得权限。 */
function isClosed(row: ConnectionCapability): boolean {
  return pendingClosed.value[row.capability] ?? row.status === 'disabled'
}

/** 已关闭项经明确授权按钮恢复；读取依赖、请求互斥和断开限制与原关闭按钮一致。 */
function cannotClose(row: ConnectionCapability): boolean {
  return (
    props.busy ||
    props.disconnected ||
    row.status === 'disabled' ||
    Boolean(blockedDependency(row.capability))
  )
}

/** 只发送一次原有关闭意图；状态标签保持服务端值，失败解除 busy 时清掉控件临时值。 */
function requestClose(row: ConnectionCapability, closed: boolean): void {
  if (!closed || cannotClose(row)) return
  pendingClosed.value = { ...pendingClosed.value, [row.capability]: true }
  emit('disable', row.capability)
}

/** 在稳定的服务端顺序上登记自有行身份，不依赖 PrimeVue 内部 class 或 DOM 结构。 */
function rowAttributes({ context }: { context: DataTableContext }) {
  const capability = props.capabilities.capabilities[context.index]?.capability
  return { 'data-testid': capability ? `capability-${capability}` : undefined }
}

/**
 * 最近验证时间按明确的 IANA 时区展示；缺失时沿用原提示，非法值不回退到宿主机时区。
 * @param value 服务端已校验的时间戳，或尚未验证时的 null。
 * @returns 带展示时区的时间，或固定缺失／不可用说明。
 */
function verifiedAt(value: string | null): string {
  if (!value) return '尚未验证'
  try {
    const formatted = new Intl.DateTimeFormat('zh-CN', {
      timeZone: props.timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).format(new Date(value))
    return `${formatted}（${props.timezone}）`
  } catch {
    return '验证时间不可用'
  }
}
const columnPt = { headerCell: { scope: 'col' } }
</script>

<template>
  <div class="min-w-0 overflow-x-auto">
    <DataTable
      :value="capabilities.capabilities"
      data-key="capability"
      :table-props="{ 'aria-label': '连接能力' }"
      :pt="{ bodyRow: rowAttributes, table: { class: 'min-w-[42rem]' } }"
    >
      <Column
        header="能力"
        :pt="columnPt"
      >
        <template #body="{ data }: { data: ConnectionCapability }">
          <strong class="break-words">{{ data.capability }}</strong>
          <p
            v-if="data.capability === 'mail.send'"
            class="mt-2 text-sm text-muted-color"
          >
            启用 mail.send 同时需要 mail.read。
          </p>
          <p
            v-if="data.capability === 'calendar.write'"
            class="mt-2 text-sm text-muted-color"
          >
            启用 calendar.write 同时需要 calendar.read。
          </p>
        </template>
      </Column>
      <Column
        header="状态"
        :pt="columnPt"
      >
        <template #body="{ data }: { data: ConnectionCapability }">
          <span role="status"><StatusTag
            kind="capability"
            :value="data.status"
          /></span>
          <!-- 暂不可用已由状态行播报，说明保留可读但关闭 Message 默认的重复即时播报。 -->
          <Message
            v-if="data.status === 'degraded'"
            severity="warn"
            role="note"
            aria-live="off"
            class="mt-2"
          >
            能力暂不可用，请重新授权或稍后刷新。
          </Message>
          <p
            v-if="data.last_error_code"
            class="mt-2 break-words text-sm"
          >
            错误代码：{{ data.last_error_code }}
          </p>
          <p
            v-if="
              capabilities.provider === 'microsoft' &&
                data.status === 'action_required'
            "
            class="mt-2 text-sm"
          >
            Microsoft
            租户可能要求管理员同意；请联系管理员批准所需委托权限，再重新授权。
          </p>
        </template>
      </Column>
      <Column
        header="授权范围"
        :pt="columnPt"
      >
        <template #body="{ data }: { data: ConnectionCapability }">
          <details>
            <summary
              class="cursor-pointer rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
            >
              实际授权范围
            </summary>
            <ul class="mt-2 space-y-1 break-all text-sm">
              <li
                v-for="scope in data.actual_scopes"
                :key="scope"
              >
                {{ scope }}
              </li>
            </ul>
            <p
              v-if="!data.actual_scopes.length"
              class="mt-2 text-sm"
            >
              尚未取得授权范围。
            </p>
            <p class="mt-2 text-sm text-muted-color">
              最近验证：{{ verifiedAt(data.last_verified_at) }}
            </p>
          </details>
        </template>
      </Column>
      <Column
        header="本地关闭"
        :pt="columnPt"
      >
        <template #body="{ data }: { data: ConnectionCapability }">
          <ToggleSwitch
            :input-id="`close-${capabilities.connection_id}-${data.capability}`"
            :aria-label="`关闭 ${data.capability}`"
            :model-value="isClosed(data)"
            :disabled="cannotClose(data)"
            @update:model-value="requestClose(data, $event)"
          />
          <p
            v-if="blockedDependency(data.capability)"
            class="mt-2 text-sm"
          >
            请先关闭 {{ blockedDependency(data.capability) }}。
          </p>
          <p
            v-if="data.status === 'disabled'"
            class="mt-2 text-sm text-muted-color"
          >
            已关闭；重新启用请使用授权按钮。
          </p>
        </template>
      </Column>
      <Column
        header="授权操作"
        :pt="columnPt"
      >
        <template #body="{ data }: { data: ConnectionCapability }">
          <!-- URL 不持久化；已启用也保留原账户恢复入口，不能把已有 scope 当成续期保证。 -->
          <Button
            :label="`${data.status === 'disabled' ? '启用' : '重新授权'} ${data.capability}`"
            severity="secondary"
            outlined
            :disabled="busy || disconnected"
            @click="emit('enable', data.capability)"
          />
        </template>
      </Column>
    </DataTable>
  </div>
</template>

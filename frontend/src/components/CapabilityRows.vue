<script setup lang="ts">
import type { CapabilityName, ConnectionCapabilities } from '@/api/types'

/** 依赖关系只控制可见操作；权限和最终拒绝仍由服务端决定。 */
const props = defineProps<{
  capabilities: ConnectionCapabilities
  busy: boolean
  disconnected: boolean
}>()
const emit = defineEmits<{
  enable: [capability: CapabilityName]
  disable: [capability: CapabilityName]
}>()
const labels = {
  disabled: '已关闭',
  authorizing: '授权中',
  enabled: '已启用',
  degraded: '暂不可用',
  action_required: '需要操作',
  revoked: '已撤销',
}

/** @param capability 读取能力名称。@returns 需先关闭的写能力，或空值。 */
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
</script>
<template>
  <ul class="capabilities">
    <li
      v-for="row in capabilities.capabilities"
      :key="row.capability"
      :data-capability="row.capability"
    >
      <div>
        <strong>{{ row.capability }}</strong> ·
        <span role="status">{{ labels[row.status] }}</span>
      </div>
      <p v-if="row.capability === 'mail.send'">
        启用 mail.send 同时需要 mail.read。
      </p>
      <p v-if="row.capability === 'calendar.write'">
        启用 calendar.write 同时需要 calendar.read。
      </p>
      <p v-if="row.last_error_code">
        错误代码：{{ row.last_error_code }}
      </p>
      <p
        v-if="
          capabilities.provider === 'microsoft' &&
            row.status === 'action_required'
        "
      >
        Microsoft
        租户可能要求管理员同意；请联系管理员批准所需委托权限，再重新授权。
      </p>
      <details>
        <summary>实际授权范围</summary>
        <ul>
          <li
            v-for="scope in row.actual_scopes"
            :key="scope"
          >
            {{ scope }}
          </li>
        </ul>
        <p v-if="!row.actual_scopes.length">
          尚未取得授权范围。
        </p>
        <p>最近验证：{{ row.last_verified_at || '尚未验证' }}</p>
      </details>
      <button
        v-if="row.status !== 'enabled'"
        type="button"
        :name="`enable-${row.capability}`"
        :disabled="busy || disconnected || row.status === 'authorizing'"
        @click="emit('enable', row.capability)"
      >
        {{ row.status === 'disabled' ? '启用' : '重新授权' }}
        {{ row.capability }}
      </button>
      <button
        v-if="row.status !== 'disabled'"
        type="button"
        :name="`disable-${row.capability}`"
        :disabled="
          busy || disconnected || Boolean(blockedDependency(row.capability))
        "
        @click="emit('disable', row.capability)"
      >
        关闭 {{ row.capability }}
      </button>
      <p v-if="blockedDependency(row.capability)">
        请先关闭 {{ blockedDependency(row.capability) }}。
      </p>
    </li>
  </ul>
</template>
<style scoped>
.capabilities {
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.75rem;
}
.capabilities > li {
  padding: 1rem;
  border: 1px solid #dce2ea;
  border-radius: 0.5rem;
  overflow-wrap: anywhere;
}
button {
  margin-top: 0.6rem;
}
details {
  margin: 0.65rem 0;
}
</style>

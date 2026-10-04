<script setup lang="ts">
import { computed, useId } from 'vue'
import Select from 'primevue/select'
import Message from 'primevue/message'
import { RouterLink } from 'vue-router'
import type { CapabilityName } from '@/api/types'
import {
  hasCapability,
  type ConnectionCatalogEntry,
} from '@/composables/useConnectionCatalog'
import { providerLabel } from '@/features/actions/presentation'

/**
 * 选择项来自本地同步目录；失效的当前账户仍可见，绝不默认切到另一个账户。
 * 原部分目录错误 alert 与能力不可用 status 逐字保留；Select 弹层内置的选中／空选项公告
 * 仅反馈本地选择，不改写连接能力或触发请求。
 */
const props = defineProps<{
  modelValue: string
  entries: ConnectionCatalogEntry[]
  capability: CapabilityName
  label: string
  disabled: boolean
}>()
defineEmits<{ 'update:modelValue': [value: string] }>()
const inputId = useId()
const labelId = `${inputId}-label`
const current = computed(() =>
  props.entries.find((entry) => entry.connection.id === props.modelValue),
)
const unavailable = computed(() => props.entries.filter((entry) => entry.error))
/** Select 显示失效原值但禁止选入，保留能力标签；任何替换都必须由用户显式选择。 */
const options = computed(() => [
  ...(!current.value
    ? [{ label: '当前账户不可用', value: props.modelValue, disabled: true }]
    : []),
  ...props.entries.map((entry) => ({
    label: `${providerLabel(entry.connection.provider)} · ${entry.connection.account_email}${hasCapability(entry, props.capability) ? '' : '（能力不可用）'}`,
    value: entry.connection.id,
    disabled: !hasCapability(entry, props.capability),
  })),
])
</script>
<template>
  <div class="grid gap-1">
    <label
      :id="labelId"
      :for="inputId"
    >{{ label }}</label>
    <Select
      :input-id="inputId"
      :aria-labelledby="labelId"
      :model-value="modelValue"
      :options="options"
      option-label="label"
      option-value="value"
      option-disabled="disabled"
      :disabled="disabled"
      :form-control="{ novalidate: true }"
      fluid
      @update:model-value="$emit('update:modelValue', $event)"
    />
  </div>
  <Message
    v-if="unavailable.length"
    severity="error"
    role="alert"
  >
    <p>部分账户能力读取失败，已保留当前选择。请到连接页面刷新后返回。</p>
    <p
      v-for="entry in unavailable"
      :key="entry.connection.id"
    >
      {{ providerLabel(entry.connection.provider)
      }}<span v-if="entry.error?.traceId">
        · 追踪编号：{{ entry.error.traceId }}</span>
    </p>
    <RouterLink to="/connections">
      查看连接与能力
    </RouterLink>
  </Message>
  <p
    v-if="current && !hasCapability(current, capability)"
    role="status"
  >
    当前账户能力不可用，请检查连接授权。
  </p>
</template>

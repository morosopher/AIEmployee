<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { CapabilityName } from '@/api/types'
import {
  hasCapability,
  type ConnectionCatalogEntry,
} from '@/composables/useConnectionCatalog'
import { providerLabel } from '@/features/actions/presentation'

/** 选择项来自本地同步目录；失效的当前账户仍可见，绝不默认切到另一个账户。 */
const props = defineProps<{
  modelValue: string
  entries: ConnectionCatalogEntry[]
  capability: CapabilityName
  label: string
  disabled: boolean
}>()
defineEmits<{ 'update:modelValue': [value: string] }>()
const current = computed(() =>
  props.entries.find((entry) => entry.connection.id === props.modelValue),
)
const unavailable = computed(() => props.entries.filter((entry) => entry.error))
</script>
<template>
  <label>{{ label }}
    <select
      :value="modelValue"
      :aria-label="label"
      :disabled="disabled"
      @change="
        $emit('update:modelValue', ($event.target as HTMLSelectElement).value)
      "
    >
      <option
        v-if="!current"
        :value="modelValue"
        disabled
      >
        当前账户不可用
      </option>
      <option
        v-for="entry in entries"
        :key="entry.connection.id"
        :value="entry.connection.id"
        :disabled="!hasCapability(entry, capability)"
      >
        {{ providerLabel(entry.connection.provider) }} ·
        {{ entry.connection.account_email
        }}{{ hasCapability(entry, capability) ? '' : '（能力不可用）' }}
      </option>
    </select>
  </label>
  <div
    v-if="unavailable.length"
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
  </div>
  <p
    v-if="current && !hasCapability(current, capability)"
    role="status"
  >
    当前账户能力不可用，请检查连接授权。
  </p>
</template>

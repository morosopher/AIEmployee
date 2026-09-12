<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import { providerLabel } from '@/features/actions/presentation'

/**
 * 将服务端明确标记缺失的连接与页面已有目录关联；不额外读取供应商或推断未知账户。
 * @param connectionIds 当前结果缺失的连接 ID，目录无法解析时仍保留精确标识。
 * @param entries 可选本地目录，能力读取失败不抹掉已经读取的账户身份。
 */
const props = withDefaults(
  defineProps<{
    connectionIds: string[]
    entries?: ConnectionCatalogEntry[]
  }>(),
  { entries: () => [] },
)
const missing = computed(() =>
  props.connectionIds.map((id) => ({
    id,
    connection: props.entries.find((entry) => entry.connection.id === id)
      ?.connection,
  })),
)
</script>
<template>
  <ul aria-label="缺失日历账户">
    <li
      v-for="item in missing"
      :key="item.id"
    >
      <template v-if="item.connection">
        {{ providerLabel(item.connection.provider) }} ·
        {{ item.connection.account_email }}
      </template>
      <template v-else>
        {{ item.id }} · 账户资料暂不可用
      </template>
    </li>
  </ul>
  <RouterLink to="/connections">
    查看连接与同步状态
  </RouterLink>
</template>

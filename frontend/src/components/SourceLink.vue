<script setup lang="ts">
import Button from 'primevue/button'

/** 来源引用只展示已提供的 HTTP(S) 地址；不构造供应商链接或执行任何写操作。 */
const props = defineProps<{
  source: {
    source_type: string
    source_id: string
    provider_url: string | null
  }
}>()
// 保留既有协议白名单与解析失败降级：无安全地址时仅显示来源文字。
const safeUrl = (() => {
  if (!props.source.provider_url) return null
  try {
    const url = new URL(props.source.provider_url)
    return url.protocol === 'http:' || url.protocol === 'https:'
      ? url.href
      : null
  } catch {
    return null
  }
})()
</script>
<template>
  <Button
    v-if="safeUrl"
    as="a"
    link
    :href="safeUrl"
    target="_blank"
    rel="noopener noreferrer"
  >
    {{ source.source_type }} 来源
  </Button>
  <span v-else>{{ source.source_type }} 来源</span>
</template>

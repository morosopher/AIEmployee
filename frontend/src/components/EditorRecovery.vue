<script setup lang="ts">
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import type { ActionRecovery } from '@/features/actions/recovery'

/**
 * 固定错误文案和显式恢复动作；创建替代对象必须来自用户点击。
 * 原单一 alert 改由 Message 承载；插槽仅补固定的本地表单说明，与原文案／traceId 共用同一公告。
 */
defineProps<{
  error: ActionRecovery | null
  busy?: boolean
  newVersionAvailable?: boolean
}>()
defineEmits<{ reload: []; newObject: []; newVersion: [] }>()
</script>
<template>
  <Message
    v-if="error"
    severity="error"
    role="alert"
    class="space-y-2"
  >
    <slot />
    <p>
      {{ error.message
      }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span>
    </p>
    <RouterLink
      v-if="error.action === 'reauthorize'"
      to="/connections"
    >
      检查连接并重新授权
    </RouterLink>
    <Button
      v-else-if="error.action === 'new_version' && newVersionAvailable"
      type="button"
      name="recover-new-version"
      :disabled="busy"
      @click="$emit('newVersion')"
    >
      创建新版本
    </Button>
    <Button
      v-else-if="error.action === 'new_object'"
      type="button"
      name="new-local-object"
      :disabled="busy"
      @click="$emit('newObject')"
    >
      新建空白对象
    </Button>
    <Button
      v-else
      type="button"
      :disabled="busy"
      @click="$emit('reload')"
    >
      重新加载后核对
    </Button>
  </Message>
</template>

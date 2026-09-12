<script setup lang="ts">
import { RouterLink } from 'vue-router'
import type { ActionRecovery } from '@/features/actions/recovery'

/** 固定错误文案和显式恢复动作；创建替代对象必须来自用户点击。 */
defineProps<{
  error: ActionRecovery | null
  busy?: boolean
  newVersionAvailable?: boolean
}>()
defineEmits<{ reload: []; newObject: []; newVersion: [] }>()
</script>
<template>
  <div
    v-if="error"
    role="alert"
    class="editor-error"
  >
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
    <button
      v-else-if="error.action === 'new_version' && newVersionAvailable"
      type="button"
      name="recover-new-version"
      :disabled="busy"
      @click="$emit('newVersion')"
    >
      创建新版本
    </button>
    <button
      v-else-if="error.action === 'new_object'"
      type="button"
      name="new-local-object"
      :disabled="busy"
      @click="$emit('newObject')"
    >
      新建空白对象
    </button>
    <button
      v-else
      type="button"
      :disabled="busy"
      @click="$emit('reload')"
    >
      重新加载后核对
    </button>
  </div>
</template>
<style scoped>
.editor-error {
  color: #a61b1b;
  padding: 0.75rem;
  border-left: 3px solid #a61b1b;
  background: #fff5f5;
}
</style>

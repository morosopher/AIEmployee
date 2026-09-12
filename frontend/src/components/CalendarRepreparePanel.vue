<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { CalendarEditorFacts, TaskStatus } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import { providerLabel } from '@/features/actions/presentation'

/** 恢复区只呈现当前来源和持久同步任务状态；所有创建/同步/重读均为显式点击。 */
const props = defineProps<{
  connectionId: string
  entries: ConnectionCatalogEntry[]
  source: CalendarEditorFacts['reprepare_source']
  busy: boolean
  canPrepare: boolean
  syncTaskId: string | null
  syncStatus: TaskStatus | null
  syncRunning: boolean
}>()
defineEmits<{ prepare: []; sync: []; refresh: [] }>()
const connection = computed(
  () =>
    props.entries.find((entry) => entry.connection.id === props.connectionId)
      ?.connection,
)
</script>
<template>
  <section aria-label="重新准备修改提案">
    <h2>重新准备修改提案</h2>
    <p>原提案和审批保留不变。新提案需要独立核对、逐项确认并提交审批。</p>
    <p v-if="connection">
      来源账户：{{ providerLabel(connection.provider) }} ·
      {{ connection.account_email }}
    </p>
    <p v-else>
      来源连接：{{ connectionId }} · 账户资料暂不可用
    </p>
    <template v-if="source">
      <p
        v-if="source.requires_sync"
        role="status"
      >
        本地日程版本尚未更新，请先同步来源账户，任务完成后重新读取提案。
      </p>
      <p
        v-if="syncTaskId"
        role="status"
      >
        {{
          syncRunning
            ? '同步进行中'
            : syncStatus === 'succeeded'
              ? '同步已完成，请重新读取提案。'
              : '同步未完成，请检查任务结果和连接。'
        }}
        <RouterLink :to="{ path: '/tasks', query: { task_id: syncTaskId } }">
          查看同步任务
        </RouterLink>
      </p>
      <div class="editor-controls">
        <button
          v-if="source.requires_sync"
          type="button"
          name="sync-reprepare-source"
          :disabled="busy || syncRunning"
          @click="$emit('sync')"
        >
          同步来源账户
        </button>
        <button
          type="button"
          name="refresh-reprepare"
          :disabled="busy || syncRunning"
          @click="$emit('refresh')"
        >
          重新读取提案
        </button>
        <button
          type="button"
          name="prepare-version"
          :disabled="!canPrepare"
          @click="$emit('prepare')"
        >
          准备新版本
        </button>
      </div>
    </template>
    <p
      v-else
      role="status"
    >
      原来源无法核实，请<RouterLink to="/brief">
        重新选择来源
      </RouterLink>。
    </p>
    <RouterLink to="/connections">
      检查连接、同步或重新授权
    </RouterLink>
  </section>
</template>

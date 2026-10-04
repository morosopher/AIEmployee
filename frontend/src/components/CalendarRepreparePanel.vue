<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Panel from 'primevue/panel'
import { RouterLink } from 'vue-router'
import type { CalendarEditorFacts, TaskStatus } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import { providerLabel } from '@/features/actions/presentation'

/**
 * 恢复区只呈现当前来源和持久同步任务状态；所有创建／同步／重读均为显式点击。
 * 原三个条件 status 的文案逐字保留，Message 显式覆盖默认 assertive，避免重复告警。
 * 确认由页面复用 AppShell 出口；本组件不生成请求意图，也不代替服务端判断 ETag。
 */
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
  <Panel>
    <template #header="{ id }">
      <h2
        :id="id"
        class="text-lg font-semibold"
      >
        重新准备修改提案
      </h2>
    </template>
    <!-- Panel 的公开 header id 为内置 region 命名，避免外层再复制同名区域。 -->
    <div class="space-y-3">
      <p>原提案和审批保留不变。新提案需要独立核对、逐项确认并提交审批。</p>
      <p v-if="connection">
        来源账户：{{ providerLabel(connection.provider) }} ·
        {{ connection.account_email }}
      </p>
      <p v-else>
        来源连接：{{ connectionId }} · 账户资料暂不可用
      </p>
      <template v-if="source">
        <Message
          v-if="source.requires_sync"
          severity="warn"
          role="status"
          aria-live="polite"
        >
          本地日程版本尚未更新，请先同步来源账户，任务完成后重新读取提案。
        </Message>
        <Message
          v-if="syncTaskId"
          severity="info"
          role="status"
          aria-live="polite"
        >
          {{
            syncRunning
              ? '同步进行中'
              : syncStatus === 'succeeded'
                ? '同步已完成，请重新读取提案。'
                : '同步未完成，请检查任务结果和连接。'
          }}
          <RouterLink
            class="text-primary underline"
            :to="{ path: '/tasks', query: { task_id: syncTaskId } }"
          >
            查看同步任务
          </RouterLink>
        </Message>
        <div class="flex flex-wrap gap-3">
          <Button
            v-if="source.requires_sync"
            type="button"
            name="sync-reprepare-source"
            :disabled="busy || syncRunning"
            @click="$emit('sync')"
          >
            同步来源账户
          </Button>
          <Button
            type="button"
            name="refresh-reprepare"
            :disabled="busy || syncRunning"
            @click="$emit('refresh')"
          >
            重新读取提案
          </Button>
          <Button
            type="button"
            name="prepare-version"
            :disabled="!canPrepare"
            @click="$emit('prepare')"
          >
            准备新版本
          </Button>
        </div>
      </template>
      <Message
        v-else
        severity="warn"
        role="status"
        aria-live="polite"
      >
        原来源无法核实，请<RouterLink
          to="/brief"
          class="text-primary underline"
        >
          重新选择来源
        </RouterLink>。
      </Message>
      <RouterLink
        to="/connections"
        class="text-primary underline"
      >
        检查连接、同步或重新授权
      </RouterLink>
    </div>
  </Panel>
</template>

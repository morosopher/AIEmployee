<script setup lang="ts">
import Message from 'primevue/message'
import { formatActionTime } from '@/features/actions/presentation'
import type { CalendarConflictPreview } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import MissingCalendarConnections from './MissingCalendarConnections.vue'

/** 只呈现服务端本人日历事实；不把缺失来源或参会人可用性伪装成已验证。 */
withDefaults(
  defineProps<{
    conflicts: CalendarConflictPreview[]
    entries?: ConnectionCatalogEntry[]
    /** 用户明确的 IANA 时区；冻结审批必须从冻结 after 传入，缺省显式 UTC。 */
    timezone?: string
  }>(),
  { timezone: 'UTC', entries: () => [] },
)
</script>
<template>
  <!-- 原 section 的 polite 公告迁到唯一 Message；内部缺失列表保持静态，避免重复播报。 -->
  <Message
    severity="warn"
    role="region"
    aria-label="日程冲突检查"
    aria-live="polite"
    class="break-words"
  >
    <p>未检查参会人可用性。</p>
    <p>冲突时间按原始时区偏移显示；Z 表示 UTC。</p>
    <ul
      v-if="conflicts.length"
      class="list-disc space-y-2 pl-5"
    >
      <li
        v-for="(conflict, index) in conflicts"
        :key="index"
      >
        <template v-if="conflict.kind === 'overlap'">
          与本人忙碌日程或缓冲时间重叠：{{ conflict.starts_at }} 至
          {{ conflict.ends_at }}
          <!-- 原 ISO 完整保留以审阅秒／微秒／offset；可读范围仅补展示，非法时区由现有函数安全降级。 -->
          <p>
            {{ timezone }}：{{
              formatActionTime(conflict.starts_at, timezone)
            }}
            至 {{ formatActionTime(conflict.ends_at, timezone) }}
          </p>
        </template>
        <template v-else-if="conflict.kind === 'outside_working_hours'">
          当前安排位于工作时间外。
        </template>
        <template v-else>
          部分来源缺失（{{ conflict.missing_connection_ids.length }}
          个连接），冲突检查可能不完整。
          <MissingCalendarConnections
            :connection-ids="conflict.missing_connection_ids"
            :entries="entries"
          />
        </template>
      </li>
    </ul>
    <p v-else>
      当前服务端检查未发现本人日历冲突。
    </p>
  </Message>
</template>

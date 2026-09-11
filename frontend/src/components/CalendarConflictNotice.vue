<script setup lang="ts">
import type { CalendarConflictPreview } from '@/api/types'

/** 只呈现服务端本人日历事实；不把缺失来源或参会人可用性伪装成已验证。 */
defineProps<{ conflicts: CalendarConflictPreview[] }>()
</script>
<template>
  <section
    aria-label="日程冲突检查"
    aria-live="polite"
  >
    <p>未检查参会人可用性。</p>
    <ul
      v-if="conflicts.length"
      class="warning"
    >
      <li
        v-for="(conflict, index) in conflicts"
        :key="index"
      >
        <template v-if="conflict.kind === 'overlap'">
          与本人忙碌日程或缓冲时间重叠：{{ conflict.starts_at }} 至
          {{ conflict.ends_at }}
        </template>
        <template v-else-if="conflict.kind === 'outside_working_hours'">
          当前安排位于工作时间外。
        </template>
        <template v-else>
          部分来源缺失（{{
            conflict.missing_connection_ids.length
          }}
          个连接），冲突检查可能不完整。
        </template>
      </li>
    </ul>
    <p v-else>
      当前服务端检查未发现本人日历冲突。
    </p>
  </section>
</template>
<style scoped>
.warning {
  background: #fff5df;
  padding: 0.75rem 0.75rem 0.75rem 1.75rem;
  border-left: 3px solid #946200;
}
</style>

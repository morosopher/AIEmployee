<script setup lang="ts">
import type { CalendarPreviewFields } from '@/api/types'

/** 前后值来自同一服务端快照；仅显示，不在客户端计算业务 diff 或命令。 */
defineProps<{
  before: CalendarPreviewFields | null
  after: CalendarPreviewFields
}>()
const fields: Array<{ key: keyof CalendarPreviewFields; label: string }> = [
  { key: 'title', label: '标题' },
  { key: 'description', label: '描述' },
  { key: 'location', label: '地点' },
  { key: 'starts_at', label: '开始' },
  { key: 'ends_at', label: '结束' },
  { key: 'timezone', label: 'IANA 时区' },
  { key: 'all_day', label: '全天' },
  { key: 'attendees', label: '参会人' },
]
/** @param value 服务端字段。@returns 不解释 HTML 的可读文本。 */
function display(
  value: CalendarPreviewFields[keyof CalendarPreviewFields] | undefined,
): string {
  if (value === null || value === undefined || value === '') return '无'
  if (Array.isArray(value)) return value.join('、') || '无'
  if (typeof value === 'boolean') return value ? '是（结束日期不含当天）' : '否'
  return value
}
</script>
<template>
  <div
    class="comparison"
    tabindex="0"
    aria-label="日程前后对比"
  >
    <table>
      <caption>
        {{
          before ? '日程修改前后' : '拟创建的日程'
        }}
      </caption>
      <thead>
        <tr>
          <th scope="col">
            字段
          </th>
          <th
            v-if="before"
            scope="col"
          >
            修改前
          </th>
          <th scope="col">
            修改后
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="field in fields"
          :key="field.key"
        >
          <th scope="row">
            {{ field.label }}
          </th>
          <td v-if="before">
            {{ display(before[field.key]) }}
          </td>
          <td>{{ display(after[field.key]) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
<style scoped>
.comparison {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
}
caption {
  text-align: left;
  font-weight: 600;
  padding: 0.5rem 0;
}
th,
td {
  text-align: left;
  padding: 0.6rem;
  vertical-align: top;
  border-bottom: 1px solid #dce2ea;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
th {
  min-width: 4rem;
}
</style>

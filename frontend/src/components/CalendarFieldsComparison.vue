<script setup lang="ts">
import { computed, useId } from 'vue'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import type { CalendarPreviewFields } from '@/api/types'

/**
 * 前后值来自同一服务端快照，既供编辑器也供冻结审批预览使用。
 * 这里只比较规范字段值以标注可读差异，不计算业务 diff、命令或审批状态。
 * before=null 仅用于调用方已经核实的新建场景；缺失的修改前快照由页面阻止展示。
 */
const props = defineProps<{
  before: CalendarPreviewFields | null
  after: CalendarPreviewFields
}>()
const captionId = useId()
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
/** 日程字段的只读值域；不引入业务命令或供应商格式。 */
type FieldValue = CalendarPreviewFields[keyof CalendarPreviewFields] | undefined

/** @returns 沿用原显示约定，null、空文本与空列表均表示未填写；字面文字“无”不是空值。 */
function isEmpty(value: FieldValue): boolean {
  return (
    value === null ||
    value === undefined ||
    value === '' ||
    (Array.isArray(value) && value.length === 0)
  )
}
/**
 * 比较规范字段值而非显示占位文案；仅统一既有空值展示，真实文本、布尔值及数组元素逐字比较。
 * @param original 只读原值。
 * @param updated 只读新值。
 * @returns 是否展示相同内容；不把差异投影回 changed_fields 或任何审批载荷。
 */
function sameValue(original: FieldValue, updated: FieldValue): boolean {
  if (isEmpty(original) || isEmpty(updated))
    return isEmpty(original) && isEmpty(updated)
  if (Array.isArray(original) && Array.isArray(updated))
    return (
      original.length === updated.length &&
      original.every((value, index) => value === updated[index])
    )
  return original === updated
}
/** @param value 服务端字段。@returns 不解释 HTML 的可读文本。 */
function display(
  value: CalendarPreviewFields[keyof CalendarPreviewFields] | undefined,
): string {
  if (value === null || value === undefined || value === '') return '无'
  if (Array.isArray(value)) return value.join('、') || '无'
  if (typeof value === 'boolean') return value ? '是（结束日期不含当天）' : '否'
  return value
}

/** 所有值均以 Vue 文本插值呈现，保留纯文本换行／空格，不解释 HTML 或 Markdown。 */
const rows = computed(() =>
  fields.map((field) => {
    const original = props.before ? display(props.before[field.key]) : null
    const updated = display(props.after[field.key])
    const changed =
      props.before !== null &&
      !sameValue(props.before[field.key], props.after[field.key])
    const change = original === null ? '拟创建' : changed ? '已变化' : '未变化'
    return {
      ...field,
      original,
      updated,
      originalEmpty: props.before !== null && isEmpty(props.before[field.key]),
      updatedEmpty: isEmpty(props.after[field.key]),
      changed,
      change,
      description: `${field.label}：${change}${changed ? '，请对比原值与新值' : ''}`,
    }
  }),
)
</script>
<template>
  <div
    class="min-w-0 space-y-2 overflow-x-auto"
    tabindex="0"
    aria-label="日程前后对比"
  >
    <p
      :id="captionId"
      class="font-semibold"
    >
      {{ before ? '日程修改前后' : '拟创建的日程' }}
    </p>
    <DataTable
      :value="rows"
      data-key="key"
      :table-props="{ 'aria-labelledby': captionId }"
      :pt="{
        bodyRow: ({ context }) => ({
          'aria-label': rows[context.index]?.description,
        }),
      }"
      :row-class="(row) => (row.changed ? 'bg-primary-50' : undefined)"
    >
      <Column
        header="字段"
        :pt="{ headerCell: { scope: 'col' }, bodyCell: { role: 'rowheader' } }"
      >
        <template #body="{ data }">
          <span class="font-medium">{{ data.label }}</span>
          <span class="mt-1 block text-sm text-muted-color">{{
            data.change
          }}</span>
        </template>
      </Column>
      <Column
        v-if="before"
        field="original"
        header="原值"
        class="whitespace-pre-wrap wrap-anywhere align-top"
        :pt="{ headerCell: { scope: 'col' } }"
      >
        <template #body="{ data }">
          <span>{{ data.original }}</span><span
            v-if="data.originalEmpty"
            class="text-muted-color"
          >（未填写）</span>
        </template>
      </Column>
      <Column
        field="updated"
        header="新值"
        class="whitespace-pre-wrap wrap-anywhere align-top"
        :pt="{ headerCell: { scope: 'col' } }"
      >
        <template #body="{ data }">
          <span>{{ data.updated }}</span><span
            v-if="data.updatedEmpty"
            class="text-muted-color"
          >（未填写）</span>
        </template>
      </Column>
    </DataTable>
  </div>
</template>

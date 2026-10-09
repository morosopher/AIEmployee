<script setup lang="ts">
import { computed } from 'vue'
import Select from 'primevue/select'
import Button from 'primevue/button'
import type { CalendarProposal } from '@/api/types'
import type { CalendarEditorForm } from '@/features/calendar/useCalendarProposalEditor'
import {
  hasCapability,
  type ConnectionCatalogEntry,
} from '@/composables/useConnectionCatalog'
import EditorConnectionSelect from './EditorConnectionSelect.vue'

/** create 可重选精确目标；update/restore 的目标来源只能显示，不能在前端松绑。 */
const props = defineProps<{
  proposal: CalendarProposal
  entries: ConnectionCatalogEntry[]
  disabled: boolean
  fieldsDirty: boolean
}>()
const form = defineModel<CalendarEditorForm>({ required: true })
defineEmits<{ confirm: [] }>()
const entry = computed(() =>
  props.entries.find((item) => item.connection.id === form.value.connection_id),
)
const calendars = computed(
  () => entry.value?.capabilities?.provider_calendars ?? [],
)
const selected = computed(() =>
  calendars.value.find((calendar) => calendar.id === form.value.calendar_id),
)
const available = computed(
  () =>
    entry.value &&
    hasCapability(entry.value, 'calendar.write') &&
    selected.value?.can_write,
)
/** 失效当前日历仍保留可读原值，不能因目录刷新自动改选另一个精确目标。 */
const options = computed(() => [
  ...(!selected.value
    ? [
        {
          label: '当前日历不可用，请明确选择',
          value: form.value.calendar_id,
          disabled: true,
        },
      ]
    : []),
  ...calendars.value.map((calendar) => ({
    label: `${calendar.name} · ${calendar.timezone}`,
    value: calendar.id,
    disabled: !calendar.can_write,
  })),
])
</script>
<template>
  <fieldset class="min-w-0 space-y-3 rounded-lg border border-surface-200 p-4">
    <legend class="font-semibold">
      精确目标日历
    </legend>
    <EditorConnectionSelect
      v-model="form.connection_id"
      :entries="entries"
      capability="calendar.write"
      label="日历账户"
      :disabled="disabled || proposal.operation_kind !== 'create'"
    />
    <!-- 目标名称和时区可能很长；列宽随 fieldset 收缩，完整选项仍可通过下拉框查看。 -->
    <div class="grid grid-cols-1 gap-1">
      <label
        id="calendar-target-label"
        for="calendar-target"
      >目标日历</label>
      <Select
        v-model="form.calendar_id"
        input-id="calendar-target"
        aria-labelledby="calendar-target-label"
        :options="options"
        option-label="label"
        option-value="value"
        option-disabled="disabled"
        :disabled="disabled || proposal.operation_kind !== 'create'"
        fluid
      />
    </div>
    <p v-if="proposal.operation_kind !== 'create'">
      来源已锁定：{{ proposal.calendar_id }} / {{ proposal.target_event_id }} ·
      ETag：{{ proposal.base_etag }}
    </p>
    <Button
      type="button"
      name="confirm-calendar"
      :disabled="disabled || fieldsDirty || !available"
      @click="$emit('confirm')"
    >
      确认此日历
    </Button>
  </fieldset>
</template>

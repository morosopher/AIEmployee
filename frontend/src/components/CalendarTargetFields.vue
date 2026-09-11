<script setup lang="ts">
import { computed } from 'vue'
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
</script>
<template>
  <fieldset>
    <legend>精确目标日历</legend>
    <EditorConnectionSelect
      v-model="form.connection_id"
      :entries="entries"
      capability="calendar.write"
      label="日历账户"
      :disabled="disabled || proposal.operation_kind !== 'create'"
    />
    <label>目标日历<select
      v-model="form.calendar_id"
      aria-label="目标日历"
      :disabled="disabled || proposal.operation_kind !== 'create'"
    >
      <option
        v-if="!selected"
        :value="form.calendar_id"
        disabled
      >
        当前日历不可用，请明确选择
      </option>
      <option
        v-for="calendar in calendars"
        :key="calendar.id"
        :value="calendar.id"
        :disabled="!calendar.can_write"
      >
        {{ calendar.name }} · {{ calendar.timezone }}
      </option>
    </select></label>
    <p v-if="proposal.operation_kind !== 'create'">
      来源已锁定：{{ proposal.calendar_id }} / {{ proposal.target_event_id }} ·
      ETag：{{ proposal.base_etag }}
    </p>
    <button
      type="button"
      name="confirm-calendar"
      :disabled="disabled || fieldsDirty || !available"
      @click="$emit('confirm')"
    >
      确认此日历
    </button>
  </fieldset>
</template>

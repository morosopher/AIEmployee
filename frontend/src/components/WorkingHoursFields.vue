<script setup lang="ts">
import Button from 'primevue/button'
import ToggleSwitch from 'primevue/toggleswitch'
import type { Weekday, WorkingHours } from '@/api/types'
import { WEEKDAYS } from '@/features/settings/schema'
import SettingsTimeInput from './SettingsTimeInput.vue'

/** 七日时间是 Form 的一个复合字段；[] 明确表示非工作日，开关不新增传输字段。 */
const props = defineProps<{
  modelValue: WorkingHours
  disabled?: boolean
  invalid?: boolean
  describedby?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [hours: WorkingHours] }>()
/** @param day 星期。@param intervals 用户编辑后的区间；保持七日完整映射且不猜默认时间。 */
function replace(day: Weekday, intervals: Array<[string, string]>): void {
  emit('update:modelValue', { ...props.modelValue, [day]: intervals })
}
/** @param day 星期。@param index 位置。@param endpoint 端点。@param value 用户原始墙上时间。 */
function change(
  day: Weekday,
  index: number,
  endpoint: 0 | 1,
  value: string,
): void {
  replace(
    day,
    props.modelValue[day].map((interval, i) =>
      i !== index
        ? interval
        : endpoint === 0
          ? [value, interval[1]]
          : [interval[0], value],
    ),
  )
}
</script>
<template>
  <fieldset
    class="min-w-0 space-y-3"
    :disabled="disabled"
    :aria-describedby="describedby"
  >
    <legend class="font-semibold">
      每周工作时间（按所选 IANA 时区）
    </legend>
    <section
      v-for="day in WEEKDAYS"
      :key="day.key"
      class="space-y-3 border-t border-surface-200 py-3"
    >
      <div class="flex items-center gap-3">
        <h3
          :id="`${day.key}-heading`"
          class="font-medium"
        >
          {{ day.label }}
        </h3>
        <ToggleSwitch
          :input-id="`${day.key}-enabled`"
          :model-value="modelValue[day.key].length > 0"
          :form-control="{ novalidate: true }"
          :disabled="disabled"
          :aria-label="`${day.label}工作日`"
          @update:model-value="replace(day.key, $event ? [['', '']] : [])"
        />
      </div>
      <p
        v-if="!modelValue[day.key].length"
        class="text-muted-color"
      >
        非工作日
      </p>
      <div
        v-for="(interval, index) in modelValue[day.key]"
        :key="index"
        class="grid min-w-0 items-end gap-3 md:grid-cols-[1fr_1fr_auto]"
      >
        <div
          v-for="endpoint in [0, 1] as const"
          :key="endpoint"
          class="grid min-w-0 gap-1"
        >
          <label
            :for="`${day.key}-${endpoint === 0 ? 'start' : 'end'}-${index}`"
          >{{ day.label }}{{ endpoint === 0 ? '开始' : '结束' }}
            {{ index + 1 }}</label>
          <SettingsTimeInput
            :input-id="`${day.key}-${endpoint === 0 ? 'start' : 'end'}-${index}`"
            :label="`${day.label}${endpoint === 0 ? '开始' : '结束'} ${index + 1}`"
            :model-value="interval[endpoint]"
            :disabled="disabled"
            :invalid="invalid"
            :describedby="describedby"
            @update:model-value="change(day.key, index, endpoint, $event)"
          />
        </div>
        <Button
          type="button"
          label="删除区间"
          severity="secondary"
          outlined
          :aria-label="`删除${day.label}区间 ${index + 1}`"
          :disabled="disabled"
          @click="
            replace(
              day.key,
              modelValue[day.key].filter((_, i) => i !== index),
            )
          "
        />
      </div>
      <Button
        type="button"
        :label="`添加${day.label}区间`"
        severity="secondary"
        outlined
        :disabled="disabled"
        @click="replace(day.key, [...modelValue[day.key], ['', '']])"
      />
    </section>
  </fieldset>
</template>

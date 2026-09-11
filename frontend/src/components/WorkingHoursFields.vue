<script setup lang="ts">
import type { Weekday, WorkingHours } from '@/api/types'

/** 每日区间以完整七日映射提交，空数组明确表示当天无工作时间。 */
const props = defineProps<{ modelValue: WorkingHours }>()
const emit = defineEmits<{ 'update:modelValue': [hours: WorkingHours] }>()
const weekdays: Array<{ key: Weekday; label: string }> = [
  { key: 'monday', label: '星期一' },
  { key: 'tuesday', label: '星期二' },
  { key: 'wednesday', label: '星期三' },
  { key: 'thursday', label: '星期四' },
  { key: 'friday', label: '星期五' },
  { key: 'saturday', label: '星期六' },
  { key: 'sunday', label: '星期日' },
]
/** @param day 星期。@param intervals 用户编辑后的区间，只更新该天。 */
function replace(day: Weekday, intervals: Array<[string, string]>): void {
  emit('update:modelValue', { ...props.modelValue, [day]: intervals })
}
/** @param day 星期。@param index 区间位置。@param endpoint 端点。@param event 原生时间输入。 */
function change(
  day: Weekday,
  index: number,
  endpoint: 0 | 1,
  event: Event,
): void {
  if (!(event.target instanceof HTMLInputElement)) return
  const value = event.target.value
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
  <fieldset>
    <legend>每周工作时间（按所选 IANA 时区）</legend>
    <section
      v-for="day in weekdays"
      :key="day.key"
      :data-weekday="day.key"
      class="working-day"
    >
      <h3>{{ day.label }}</h3>
      <p v-if="!modelValue[day.key].length">
        非工作日
      </p>
      <div
        v-for="(interval, index) in modelValue[day.key]"
        :key="index"
        class="interval"
      >
        <label>{{ day.label }}开始 {{ index + 1
        }}<input
          :name="`${day.key}-start-${index}`"
          :value="interval[0]"
          type="time"
          required
          @input="change(day.key, index, 0, $event)"
        ></label>
        <label>{{ day.label }}结束 {{ index + 1
        }}<input
          :name="`${day.key}-end-${index}`"
          :value="interval[1]"
          type="time"
          required
          @input="change(day.key, index, 1, $event)"
        ></label>
        <button
          type="button"
          :aria-label="`删除${day.label}区间 ${index + 1}`"
          @click="
            replace(
              day.key,
              modelValue[day.key].filter((_, i) => i !== index),
            )
          "
        >
          删除区间
        </button>
      </div>
      <button
        type="button"
        @click="replace(day.key, [...modelValue[day.key], ['', '']])"
      >
        添加{{ day.label }}区间
      </button>
    </section>
  </fieldset>
</template>
<style scoped>
.working-day {
  border-top: 1px solid #dce2ea;
  padding: 0.75rem 0;
}
.interval {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}
label {
  display: grid;
  gap: 0.3rem;
}
h3 {
  font-size: 1rem;
}
</style>

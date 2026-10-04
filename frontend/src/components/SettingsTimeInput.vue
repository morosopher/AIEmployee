<script setup lang="ts">
import { computed, nextTick } from 'vue'
import DatePicker from 'primevue/datepicker'

/**
 * timeOnly 控件的字符串边界：Date 仅承载小时／分钟，不是业务日期；不进行 UTC 或时区转换。
 * 无效手工文本同样向表单传递，禁止 DatePicker 的解析失败吞掉新输入、提交旧的合法时间。
 */
const props = defineProps<{
  modelValue: string
  inputId: string
  label: string
  disabled?: boolean
  invalid?: boolean
  describedby?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const value = computed(() => {
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(props.modelValue)) return null
  const [hour = 0, minute = 0] = props.modelValue.split(':').map(Number)
  return new Date(2000, 0, 1, hour, minute)
})
/** @param date 控件明确选出的时间载体；仅读其本地小时／分钟，不产生 ISO 日期。 */
function choose(
  date: Date | Date[] | (Date | null)[] | null | undefined,
): void {
  if (date instanceof Date)
    emit(
      'update:modelValue',
      `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`,
    )
  else if (date == null) emit('update:modelValue', '')
}
/** @param event 用户当前原始文本；格式错误交给 zod，而不是回退为旧值。 */
function input(event: Event): void {
  if (event.target instanceof HTMLInputElement)
    emit('update:modelValue', event.target.value)
}
/**
 * PrimeVue 4.5.5 blur 会以最后可解析值重写 input；下一 tick 恢复当前草稿，保持错误可见。
 * @param event 公开 blur 事件；只使用原生 target，不查找组件内部 DOM。
 */
async function preserveText(event: { originalEvent: Event }): Promise<void> {
  const target = event.originalEvent.target
  await nextTick()
  if (target instanceof HTMLInputElement && target.isConnected)
    target.value = props.modelValue
}
</script>
<template>
  <DatePicker
    :model-value="value"
    :input-id="inputId"
    :disabled="disabled"
    :invalid="invalid"
    :form-control="{ novalidate: true }"
    time-only
    hour-format="24"
    :show-on-focus="false"
    show-icon
    icon="pi pi-clock"
    fluid
    :pt="{
      pcInputText: {
        root: {
          value: modelValue,
          'aria-describedby': describedby,
          'aria-invalid': invalid || undefined,
          inputmode: 'numeric',
        },
      },
      dropdown: {
        'aria-label': `选择${label}`,
      },
      dropdownIcon: { 'aria-hidden': true },
    }"
    @update:model-value="choose"
    @input="input"
    @blur="preserveText"
  />
</template>

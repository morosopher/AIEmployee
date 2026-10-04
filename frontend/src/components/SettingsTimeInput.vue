<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
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
  // 4.5.5 空值会回退宿主当前日期，DST 缺失小时可把每日 02:30 归一为 03:30。
  // 00:00 只定位时钟，不是默认业务值；syncDisplay 保留用户原字符串且绝不 emit。
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(props.modelValue))
    return new Date(2000, 0, 1, 0, 0)
  const [hour = 0, minute = 0] = props.modelValue.split(':').map(Number)
  return new Date(2000, 0, 1, hour, minute)
})
/** 当前实例开始卸载后停止显示同步，避免同 ID 新控件被旧 nextTick 回调改写。 */
let disposed = false
onBeforeUnmount(() => {
  disposed = true
})
/**
 * DatePicker mounted／model watcher 会用载体重写输入；公开 inputId 只定位当前实例，
 * Vue 更新后恢复唯一原字符串。挂载、父级换值和打开弹层不产生业务回写。
 */
async function syncDisplay(): Promise<void> {
  if (disposed) return
  const target = document.getElementById(props.inputId)
  await nextTick()
  if (
    !disposed &&
    target instanceof HTMLInputElement &&
    target.isConnected &&
    target.value !== props.modelValue
  )
    target.value = props.modelValue
}
onMounted(syncDisplay)
watch(() => props.modelValue, syncDisplay, { flush: 'post' })
/** @param date 控件明确选出的时间载体；仅读其本地小时／分钟，不产生 ISO 日期。 */
function choose(
  date: Date | Date[] | (Date | null)[] | null | undefined,
): void {
  if (disposed) return
  if (date instanceof Date)
    emit(
      'update:modelValue',
      `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`,
    )
  else if (date == null) emit('update:modelValue', '')
}
/** @param event 用户当前原始文本；格式错误交给 zod，而不是回退为旧值。 */
function input(event: Event): void {
  if (!disposed && event.target instanceof HTMLInputElement)
    emit('update:modelValue', event.target.value)
}
/**
 * PrimeVue 4.5.5 blur 会以最后可解析值重写 input；下一 tick 恢复当前草稿，保持错误可见。
 * @param event 公开 blur 事件；只使用原生 target，不查找组件内部 DOM。
 */
async function preserveText(event: { originalEvent: Event }): Promise<void> {
  const target = event.originalEvent.target
  await nextTick()
  if (!disposed && target instanceof HTMLInputElement && target.isConnected)
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
      // 锚定时间弹层允许继续编辑外部组合输入，与日程控件一致，不声明背景 inert。
      panel: { 'aria-modal': false },
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

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import DatePicker, { type DatePickerDateContext } from 'primevue/datepicker'
import CalendarWallTimeFields from './CalendarWallTimeFields.vue'

/**
 * DatePicker 的 Date 仅承载日历年月日；时分秒始终为字符串，禁止转 ISO 推断宿主区。
 * 原始手输总写回上层字符串，宿主 DST 缺失小时也不被归一成另一时刻；业务唯一性由原 time.ts 判断。
 * 挂载只计算显示值，不 emit，因此未修改字段的原始秒／小数秒仍由 hook 的基线保留。
 */
const props = defineProps<{
  modelValue: string
  allDay: boolean
  inputId: string
  label: string
  disabled: boolean
  invalid?: boolean
  describedby?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
/** 离场 portal 可能仍持有旧日格节点；实例开始卸载即停止所有回写，不能按同 ID 触及新表单。 */
let disposed = false
onBeforeUnmount(() => {
  disposed = true
})
const value = computed(() => {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?=T|$)/.exec(props.modelValue)
  if (!match) return null
  const [, y = '', m = '', d = ''] = match
  // Date 仅让日历显示对应年月日；中午载体不承载用户的小时，永不回流 form。
  const date = new Date(2000, 0, 1, 12)
  date.setFullYear(Number(y), Number(m) - 1, Number(d))
  if (
    date.getFullYear() !== Number(y) ||
    date.getMonth() + 1 !== Number(m) ||
    date.getDate() !== Number(d)
  )
    return null
  return date
})
/**
 * DatePicker 4.5.5 的 mounted/modelValue watcher 会直接写原生输入，早于或绕过 PT 的 value。
 * 用公开 inputId 在子组件更新后恢复唯一墙上字符串；仅不同才写，避免焦点时重写值而打断全选。
 * 不读取内部实例/DOM 层级，也不向 form emit，故初始化与原精度保持只读。
 */
async function syncDisplay(): Promise<void> {
  if (disposed) return
  const target = document.getElementById(props.inputId)
  await nextTick()
  // 捕获本次实例的公开输入元素；卸载后即使新表单复用相同 ID，也不能写入新实例。
  if (
    !disposed &&
    target instanceof HTMLInputElement &&
    target.isConnected &&
    target.value !== props.modelValue
  )
    target.value = props.modelValue
}
onMounted(syncDisplay)
watch(() => [props.modelValue, props.allDay], syncDisplay, { flush: 'post' })
/**
 * @param date 公开 PT context 中用户实际点击／按 Enter 或 Space 的日格数字。
 * 原生 DatePicker 随后可能归一宿主 DST；因此只信任此年月日，保留用户未编辑的原时间后缀。
 */
function selectDay(date: DatePickerDateContext): void {
  if (disposed || props.disabled || !date.selectable) return
  const day = `${String(date.year).padStart(4, '0')}-${String(date.month + 1).padStart(2, '0')}-${String(date.day).padStart(2, '0')}`
  const suffix = /^\d{4}-\d{2}-\d{2}T/.test(props.modelValue)
    ? props.modelValue.slice(10)
    : ''
  emit('update:modelValue', props.allDay ? day : `${day}${suffix}`)
  // 同值重选不会触发 model watcher，也须在原生日格处理后恢复组合输入的原字符串。
  void syncDisplay()
}
/** @param event 日格本身的公开键盘事件；方向键只移动焦点，不提交时间或改变日期。 */
function selectDayByKey(
  event: KeyboardEvent,
  date: DatePickerDateContext,
): void {
  if (['Enter', 'NumpadEnter', 'Space'].includes(event.code)) selectDay(date)
}
/**
 * @param event 当前语义 dialog 内尚未被日格／头部处理的 Tab；保留原 clock 的正反焦点循环。
 * 只枚举本弹层真实可见、启用的控件，不调用私有 trapFocus，也不处理已 preventDefault 的事件。
 */
function cycleFooterFocus(event: KeyboardEvent): void {
  if (disposed || event.code !== 'Tab' || event.defaultPrevented) return
  const panel = event.currentTarget
  if (
    !(panel instanceof HTMLElement) ||
    panel.getAttribute('role') !== 'dialog'
  )
    return
  const controls = Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button, input, select, textarea, a[href], [tabindex]',
    ),
  ).filter(
    (control) =>
      control.tabIndex >= 0 &&
      !control.matches(':disabled, [aria-disabled="true"]') &&
      !control.closest('[hidden], [inert], [aria-hidden="true"]') &&
      control.getClientRects().length > 0 &&
      getComputedStyle(control).visibility !== 'hidden',
  )
  if (!controls.length) return
  const active = document.activeElement
  const index = controls.findIndex((control) => control === active)
  const next = event.shiftKey
    ? index <= 0
      ? controls.length - 1
      : index - 1
    : (index + 1) % controls.length
  event.preventDefault()
  controls[next]?.focus()
}
/** @param event 当前文本包含无效输入也必须同步；只接受可见日期和时间之间的空格为 T 的等价表示。 */
function input(event: Event): void {
  if (!disposed && event.target instanceof HTMLInputElement)
    emit(
      'update:modelValue',
      event.target.value.replace(/^(\d{4}-\d{2}-\d{2}) (\d{2}:)/, '$1T$2'),
    )
}
/** @param event 4.5.5 blur 后会回填最后可解析 Date，下一 tick 恢复用户真实字符串，禁止错误被旧值遮蔽。 */
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
    :show-time="!allDay"
    :show-seconds="!allDay"
    :timepicker-button-props="{ disabled: true, tabindex: -1 }"
    hour-format="24"
    date-format="yy-mm-dd"
    :form-control="{ novalidate: true }"
    :show-on-focus="false"
    show-icon
    fluid
    :pt="{
      pcInputText: {
        root: {
          value: modelValue,
          inputmode: 'text',
          'aria-label': label,
          'aria-describedby': describedby,
          'aria-invalid': invalid || undefined,
        },
      },
      // 锚定弹层允许继续编辑外部输入，不声明背景 inert 的模态语义。
      panel: { 'aria-modal': false, onKeydown: cycleFooterFocus },
      // 原生 clock 经宿主 Date.setHours 会归一 DST；其按钮另由 timepickerButtonProps 禁用并移出焦点枚举，footer 编辑原数字。
      timePicker: {
        hidden: true,
        'aria-hidden': true,
        style: { display: 'none' },
      },
      dropdown: { 'aria-label': `选择${label}` },
      dropdownIcon: { 'aria-hidden': true },
      // 4.5.5 把 aria-selected 写在普通 span；选择语义属于 gridcell，公开 PT 修复而不改组件状态。
      dayCell: ({ context }) => ({
        role: 'gridcell',
        'aria-selected': context.selected,
      }),
      day: ({ context }) => ({
        role: 'button',
        onClickCapture: () => selectDay(context.date),
        onKeydownCapture: (event: KeyboardEvent) =>
          selectDayByKey(event, context.date),
        // 4.5.5 的旧选择器未给当前日格设 tabindex；保留一个可键盘进入的选中／今日入口。
        tabindex: context.selected || (!value && context.today) ? 0 : -1,
        'aria-selected': undefined,
        'aria-label': `${context.date.year}-${String(context.date.month + 1).padStart(2, '0')}-${String(context.date.day).padStart(2, '0')}`,
      }),
    }"
    @input="input"
    @blur="preserveText"
  >
    <template #footer>
      <CalendarWallTimeFields
        v-if="!allDay"
        :model-value="modelValue"
        :input-id="`${inputId}-clock`"
        :label="label"
        :disabled="disabled"
        :describedby="describedby"
        @update:model-value="emit('update:modelValue', $event)"
      />
    </template>
  </DatePicker>
</template>

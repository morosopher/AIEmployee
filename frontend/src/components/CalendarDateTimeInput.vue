<script setup lang="ts">
import { computed, nextTick, onMounted, watch } from 'vue'
import DatePicker from 'primevue/datepicker'

/**
 * DatePicker 的 Date 仅承载日历格及墙上数字，不是业务时刻；禁止转 ISO 推断宿主区。
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
const value = computed(() => {
  const match =
    /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?$/.exec(
      props.modelValue,
    )
  if (!match) return null
  const [, y = '', m = '', d = '', h = '0', minute = '0', sec = '0'] = match
  const date = new Date(2000, 0, 1, 12)
  date.setFullYear(Number(y), Number(m) - 1, Number(d))
  date.setHours(Number(h), Number(minute), Number(sec), 0)
  // 构造载体若遇宿主 DST 跳跃，保留文本且不向控件提供错位的可选时间。
  if (
    date.getFullYear() !== Number(y) ||
    date.getMonth() + 1 !== Number(m) ||
    date.getDate() !== Number(d) ||
    date.getHours() !== Number(h) ||
    date.getMinutes() !== Number(minute) ||
    date.getSeconds() !== Number(sec)
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
  const target = document.getElementById(props.inputId)
  await nextTick()
  // 捕获本次实例的公开输入元素；卸载后即使新表单复用相同 ID，也不能写入新实例。
  if (
    target instanceof HTMLInputElement &&
    target.isConnected &&
    target.value !== props.modelValue
  )
    target.value = props.modelValue
}
onMounted(syncDisplay)
watch(() => [props.modelValue, props.allDay], syncDisplay, { flush: 'post' })
/** @param date 用户在日历弹层明确选择的载体；只读年月日和时分秒，格式不携带宿主 offset。 */
function choose(
  date: Date | Date[] | (Date | null)[] | null | undefined,
): void {
  if (date instanceof Date) {
    const pad = (part: number) => String(part).padStart(2, '0')
    const day = `${String(date.getFullYear()).padStart(4, '0')}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    const time = `${pad(date.getHours())}:${pad(date.getMinutes())}${date.getSeconds() ? `:${pad(date.getSeconds())}` : ''}`
    emit('update:modelValue', props.allDay ? day : `${day}T${time}`)
  } else if (date == null) emit('update:modelValue', '')
}
/** @param event 当前文本包含无效输入也必须同步；只接受可见日期和时间之间的空格为 T 的等价表示。 */
function input(event: Event): void {
  if (event.target instanceof HTMLInputElement)
    emit(
      'update:modelValue',
      event.target.value.replace(/^(\d{4}-\d{2}-\d{2}) (\d{2}:)/, '$1T$2'),
    )
}
/** @param event 4.5.5 blur 后会回填最后可解析 Date，下一 tick 恢复用户真实字符串，禁止错误被旧值遮蔽。 */
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
    :show-time="!allDay"
    :show-seconds="!allDay"
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
      panel: { 'aria-modal': false },
      dropdown: { 'aria-label': `选择${label}` },
      dropdownIcon: { 'aria-hidden': true },
      // 4.5.5 把 aria-selected 写在普通 span；选择语义属于 gridcell，公开 PT 修复而不改组件状态。
      dayCell: ({ context }) => ({
        role: 'gridcell',
        'aria-selected': context.selected,
      }),
      day: ({ context }) => ({
        role: 'button',
        // 4.5.5 的旧选择器未给当前日格设 tabindex；保留一个可键盘进入的选中／今日入口。
        tabindex: context.selected || (!value && context.today) ? 0 : -1,
        'aria-selected': undefined,
        'aria-label': `${context.date.year}-${String(context.date.month + 1).padStart(2, '0')}-${String(context.date.day).padStart(2, '0')}`,
      }),
    }"
    @update:model-value="choose"
    @input="input"
    @blur="preserveText"
  />
</template>

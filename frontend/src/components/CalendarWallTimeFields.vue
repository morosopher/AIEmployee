<script setup lang="ts">
import { computed } from 'vue'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'

/**
 * 日程 DatePicker footer 的墙上时钟；所有数字直接组成原输入字符串，绝不经过宿主 Date。
 * 无效／未完整值不钳制、不取整，立即向原 form 传播，由外层 schema 统一报告格式错误。
 * 仅有日期时不自动设置小时／分钟；只读显示的零秒不改变省略零秒的原始基线。
 */
const props = defineProps<{
  modelValue: string
  inputId: string
  label: string
  disabled: boolean
  describedby?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const fields = [
  {
    key: 'hour',
    label: '小时',
    previous: '上一小时',
    next: '下一小时',
    max: 23,
  },
  {
    key: 'minute',
    label: '分钟',
    previous: '上一分钟',
    next: '下一分钟',
    max: 59,
  },
  { key: 'second', label: '秒', previous: '上一秒', next: '下一秒', max: 59 },
] as const
type TimePart = (typeof fields)[number]['key']
const date = computed(
  () => /^\d{4}-\d{2}-\d{2}(?=T|$)/.exec(props.modelValue)?.[0] ?? '',
)
const parts = computed(() => {
  const time = props.modelValue.slice(date.value.length).replace(/^T/, '')
  const [hour = '', minute = '', ...seconds] = time.split(':')
  return {
    hour,
    minute,
    second: seconds.length ? seconds.join(':') : '00',
    explicitSeconds: seconds.length > 0,
  }
})
/** @param key 用户编辑的数字位；无效字符串亦完整保留，不以另一个有效值替换。 */
function updatePart(key: TimePart, value: string): void {
  if (props.disabled || !date.value || value === parts.value[key]) return
  const next = { ...parts.value, [key]: value }
  // 秒从暂时不完整的 0 回到 00 时也恢复省略形式，避免同值重输破坏已保存 fold 时刻的 offset。
  // 编辑小时／分钟则保持未编辑的秒后缀，包含无效文本，交由 schema 报错。
  const withSeconds = key === 'second' ? value !== '00' : next.explicitSeconds
  emit(
    'update:modelValue',
    `${date.value}T${next.hour}:${next.minute}${withSeconds ? `:${next.second}` : ''}`,
  )
}
/** @returns 只对当前完整合法数字启用步进，不能把 24、1.5 或空白强行转换成可保存时间。 */
function canStep(key: TimePart, max: number): boolean {
  return (
    !!date.value &&
    /^\d{2}$/.test(parts.value[key]) &&
    Number(parts.value[key]) <= max
  )
}
/** @param direction 一次明确点击的正／反步进；沿用原时钟同位回绕，不自动改变其他数字或日期。 */
function step(key: TimePart, max: number, direction: -1 | 1): void {
  if (!canStep(key, max)) return
  updatePart(
    key,
    String(
      (Number(parts.value[key]) + direction + max + 1) % (max + 1),
    ).padStart(2, '0'),
  )
}
</script>
<template>
  <fieldset
    :aria-label="`${label}时分秒`"
    class="min-w-0 border-t border-surface-200 pt-3"
  >
    <legend class="text-sm font-medium">
      {{ label }}时分秒
    </legend>
    <p
      v-if="!date"
      class="text-sm"
    >
      请先选择日期，再填写时分秒。
    </p>
    <div class="grid grid-cols-3 gap-2">
      <div
        v-for="field in fields"
        :key="field.key"
        class="grid min-w-0 gap-1"
      >
        <label
          :for="`${inputId}-${field.key}`"
          class="text-center text-sm"
        >{{
          field.label
        }}</label>
        <Button
          type="button"
          :aria-label="field.next"
          icon="pi pi-chevron-up"
          severity="secondary"
          text
          size="small"
          :disabled="disabled || !canStep(field.key, field.max)"
          @click="step(field.key, field.max, 1)"
        />
        <InputText
          :id="`${inputId}-${field.key}`"
          :model-value="parts[field.key]"
          :aria-label="`${label}${field.label}`"
          :aria-describedby="describedby"
          inputmode="numeric"
          :form-control="{ novalidate: true }"
          :disabled="disabled || !date"
          class="min-w-0 text-center"
          fluid
          @update:model-value="updatePart(field.key, $event ?? '')"
        />
        <Button
          type="button"
          :aria-label="field.previous"
          icon="pi pi-chevron-down"
          severity="secondary"
          text
          size="small"
          :disabled="disabled || !canStep(field.key, field.max)"
          @click="step(field.key, field.max, -1)"
        />
      </div>
    </div>
  </fieldset>
</template>

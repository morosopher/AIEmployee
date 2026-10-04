/** 墙上时间控件只编辑原字符串，不使用 Date、不钳制输入、不创建日期或默认小时。 */
import { describe, expect, it } from 'vitest'
import { fireEvent } from '@testing-library/vue'
import { defineComponent, h, ref } from 'vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import CalendarWallTimeFields from './CalendarWallTimeFields.vue'

async function renderClock(initial: string) {
  const value = ref(initial)
  const view = await renderWithPlugins(
    defineComponent({
      render: () =>
        h(CalendarWallTimeFields, {
          modelValue: value.value,
          inputId: 'synthetic-time',
          label: '开始时间',
          disabled: false,
          'onUpdate:modelValue': (next: string) => {
            value.value = next
          },
        }),
    }),
  )
  return { view, value }
}
describe('CalendarWallTimeFields', () => {
  it('does not normalize absent seconds or invent missing hours on display', async () => {
    const { view, value } = await renderClock('2030-03-10')
    expect(view.getByLabelText('开始时间小时')).toHaveValue('')
    expect(view.getByLabelText('开始时间分钟')).toHaveValue('')
    expect(value.value).toBe('2030-03-10')
    await fireEvent.update(view.getByLabelText('开始时间秒'), '00')
    expect(value.value).toBe('2030-03-10')
    expect(view.getByRole('button', { name: '下一小时' })).toBeDisabled()
    await fireEvent.update(view.getByLabelText('开始时间小时'), '02')
    expect(value.value).toBe('2030-03-10T02:')
    await fireEvent.update(view.getByLabelText('开始时间分钟'), '30')
    expect(value.value).toBe('2030-03-10T02:30')
  })
  it.each(['24', '1.5', '', 'x'])(
    'preserves invalid hours %s instead of clamping or rounding',
    async (hours) => {
      const { view, value } = await renderClock('2030-03-10T02:30')
      await fireEvent.update(view.getByLabelText('开始时间小时'), hours)
      expect(value.value).toBe(`2030-03-10T${hours}:30`)
      expect(view.getByRole('button', { name: '下一小时' })).toBeDisabled()
    },
  )
  it('retains the unedited invalid seconds suffix when another digit changes', async () => {
    const { view, value } = await renderClock('2030-03-10T02:30:bad:seconds')
    await fireEvent.update(view.getByLabelText('开始时间小时'), '03')
    expect(value.value).toBe('2030-03-10T03:30:bad:seconds')
  })
  it('returns to the omitted-seconds baseline after typing the same zero seconds digit by digit', async () => {
    const { view, value } = await renderClock('2030-03-10T02:30')
    await fireEvent.update(view.getByLabelText('开始时间秒'), '0')
    expect(value.value).toBe('2030-03-10T02:30:0')
    await fireEvent.update(view.getByLabelText('开始时间秒'), '00')
    expect(value.value).toBe('2030-03-10T02:30')
  })
  it('keeps same zero seconds unchanged and applies explicit steps without a host date', async () => {
    const { view, value } = await renderClock('2030-03-10T01:30')
    await fireEvent.update(view.getByLabelText('开始时间秒'), '00')
    expect(value.value).toBe('2030-03-10T01:30')
    await fireEvent.click(view.getByRole('button', { name: '下一小时' }))
    expect(value.value).toBe('2030-03-10T02:30')
    await fireEvent.click(view.getByRole('button', { name: '下一分钟' }))
    expect(value.value).toBe('2030-03-10T02:31')
    await fireEvent.click(view.getByRole('button', { name: '下一秒' }))
    expect(value.value).toBe('2030-03-10T02:31:01')
  })
})

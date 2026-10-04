/** 真实 timeOnly 组件回归；只替换系统 Date，时间载体不能成为未经选择的业务默认值。 */
import { fireEvent, waitFor } from '@testing-library/vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref } from 'vue'
import SettingsTimeInput from './SettingsTimeInput.vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'

afterEach(() => vi.useRealTimers())

/** @param initial 原始表单字符串；真实父子 v-model 回路记录所有主动回写。 */
async function renderTime(initial: string) {
  const raw = ref(initial)
  const updates: string[] = []
  const Host = defineComponent({
    setup() {
      return () =>
        h('div', [
          h('label', { for: 'working-time' }, '工作时间'),
          h(SettingsTimeInput, {
            modelValue: raw.value,
            inputId: 'working-time',
            label: '工作时间',
            'onUpdate:modelValue': (value: string) => {
              raw.value = value
              updates.push(value)
            },
          }),
        ])
    },
  })
  const view = await renderWithPlugins(Host)
  return { ...view, raw, updates }
}

/** 按下／抬起都不可省略：PrimeVue 在抬起时回写，测试不能调用组件私有方法。 */
async function increment(button: HTMLElement, times: number): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await fireEvent.mouseDown(button)
    await fireEvent.mouseUp(button)
  }
}

describe('SettingsTimeInput stable wall time carrier', () => {
  it.each(['', '25:00'])(
    'keeps raw %s until the user explicitly chooses 02:30',
    async (initial) => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime('2026-03-08T06:30:00Z')
      const view = await renderTime(initial)
      const input = view.getByLabelText('工作时间')
      await waitFor(() => expect(input).toHaveValue(initial))
      for (const value of ['25:00', 'invalid-time', initial]) {
        view.raw.value = value
        await waitFor(() => expect(input).toHaveValue(value))
        await fireEvent.blur(input)
        await waitFor(() => expect(input).toHaveValue(value))
        expect(view.updates).toEqual([])
      }
      await fireEvent.click(view.getByRole('button', { name: '选择工作时间' }))
      await view.findByRole('dialog')
      expect(input).toHaveValue(initial)
      expect(view.updates).toEqual([])
      await increment(view.getByRole('button', { name: '下一小时' }), 2)
      await increment(view.getByRole('button', { name: '下一分钟' }), 30)
      await waitFor(() => expect(input).toHaveValue('02:30'))
      expect(view.updates.at(-1)).toBe('02:30')
    },
  )

  it('declares an anchored nonmodal popup and restores the input on Escape', async () => {
    const view = await renderTime('09:00')
    await fireEvent.click(view.getByRole('button', { name: '选择工作时间' }))
    const dialog = await view.findByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'false')
    const hour = view.getByRole('button', { name: '下一小时' })
    hour.focus()
    await fireEvent.keyDown(hour, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    expect(view.getByLabelText('工作时间')).toHaveFocus()
    expect(view.updates).toEqual([])
  })

  it('never lets a pending display sync overwrite a replacement with the same input id', async () => {
    const view = await renderTime('')
    view.raw.value = 'queued-invalid'
    await nextTick()
    view.unmount()
    const replacement = document.createElement('input')
    replacement.id = 'working-time'
    replacement.value = 'new-instance'
    document.body.append(replacement)
    await nextTick()
    expect(replacement.value).toBe('new-instance')
    expect(view.updates).toEqual([])
    replacement.remove()
  })
})

import { describe, expect, it } from 'vitest'
import { defineComponent, h, inject, ref } from 'vue'
import { fireEvent, waitFor } from '@testing-library/vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { RouterLink } from 'vue-router'

import { renderWithPlugins, setViewport } from './renderWithPlugins'

describe('renderWithPlugins', () => {
  it('renders PrimeVue components with the app plugins and jest-dom matchers', async () => {
    const { getByRole } = await renderWithPlugins(Button, {
      props: { label: '保存设置' },
    })
    expect(getByRole('button', { name: '保存设置' })).toBeEnabled()
  })

  it('navigates the stub router to the requested route before rendering', async () => {
    const { router } = await renderWithPlugins(Button, {
      props: { label: '操作中心' },
      route: '/actions?group=approval',
    })
    expect(router.currentRoute.value.fullPath).toBe('/actions?group=approval')
  })

  it('marks only the link of the current route with aria-current under the stub router', async () => {
    const Navigation = defineComponent({
      render: () =>
        h('nav', { 'aria-label': '主导航' }, [
          h(RouterLink, { to: '/actions' }, () => '操作中心'),
          h(RouterLink, { to: '/chat' }, () => '对话'),
        ]),
    })
    const { getByRole } = await renderWithPlugins(Navigation, { route: '/actions' })
    expect(getByRole('link', { name: '操作中心' })).toHaveAttribute('aria-current', 'page')
    expect(getByRole('link', { name: '对话' })).not.toHaveAttribute('aria-current')
  })

  it('merges caller plugins, stubs and slots with the app plugins', async () => {
    const injectedKey = Symbol('synthetic-caller-plugin')
    const ChildWidget = defineComponent({
      name: 'ChildWidget',
      render: () => h('p', '真实子组件'),
    })
    const Harness = defineComponent({
      setup(_, { slots }) {
        const injected = inject(injectedKey, '缺少调用方插件')
        return () =>
          h('section', { 'aria-label': '合并检查' }, [
            h('p', injected),
            h(ChildWidget),
            slots.default?.(),
          ])
      },
    })
    const { getByRole, getByText, queryByText, pinia, router } =
      await renderWithPlugins(Harness, {
        slots: { default: () => h('p', '调用方插槽') },
        global: {
          plugins: [
            { install: (app) => app.provide(injectedKey, '调用方插件已安装') },
          ],
          stubs: { ChildWidget: { render: () => h('p', '调用方替身') } },
        },
      })
    expect(getByRole('region', { name: '合并检查' })).toBeInTheDocument()
    expect(getByText('调用方插件已安装')).toBeInTheDocument()
    expect(getByText('调用方替身')).toBeInTheDocument()
    expect(queryByText('真实子组件')).toBeNull()
    expect(getByText('调用方插槽')).toBeInTheDocument()
    // 默认插件仍然生效：调用方插件是追加而不是替换。
    expect(pinia.state.value).toEqual({})
    expect(router.currentRoute.value.fullPath).toBe('/')
  })

  it('runs real PrimeVue transitions so modal dialogs move focus, close on Escape and restore focus', async () => {
    const ConfirmHarness = defineComponent({
      setup() {
        const visible = ref(false)
        return () => [
          h(Button, {
            label: '打开确认',
            onClick: () => {
              visible.value = true
            },
          }),
          h(
            Dialog,
            {
              visible: visible.value,
              'onUpdate:visible': (value: boolean) => {
                visible.value = value
              },
              modal: true,
              header: '确认操作',
            },
            { default: () => h('p', '确认后才会执行。') },
          ),
        ]
      },
    })
    const { getByRole, findByRole, queryByRole } =
      await renderWithPlugins(ConfirmHarness)
    const trigger = getByRole('button', { name: '打开确认' })
    // fireEvent.click 不会像真实指针点击那样移动焦点；先聚焦触发按钮，Dialog 才能
    // 在进入过渡时记住它，并在关闭后把焦点还给它。
    trigger.focus()
    await fireEvent.click(trigger)

    const dialog = await findByRole('dialog', { name: '确认操作' })
    // 进入过渡结束后 Dialog 把焦点移入对话框；关闭按钮名称来自 zh-CN aria 文案。
    await waitFor(() => expect(getByRole('button', { name: '关闭' })).toHaveFocus())
    expect(dialog).toContainElement(getByRole('button', { name: '关闭' }))

    // PrimeVue 只识别 event.code === 'Escape'，只带 key 的事件会被忽略。
    await fireEvent.keyDown(document.activeElement ?? document.body, {
      key: 'Escape',
      code: 'Escape',
    })
    await waitFor(() => expect(queryByRole('dialog')).toBeNull())
    expect(trigger).toHaveFocus()
  })

  it('installs the viewport stub at the jsdom default width and re-exports setViewport', async () => {
    // DatePicker、Select 等在 mounted 中直接调用 matchMedia；未调用 setViewport 时也必须可用。
    await renderWithPlugins(Button, { props: { label: '保存设置' } })
    expect(window.matchMedia('(min-width: 1024px)').matches).toBe(true)
    expect(window.matchMedia('(min-width: 1024.1px)').matches).toBe(false)
    // 后续组件测试统一从 renderWithPlugins 导入 setViewport。
    setViewport(600)
    expect(window.matchMedia('(max-width: 767.9px)').matches).toBe(true)
  })
})

/** 登录页回归使用真实认证 Store 与 API 解析器；仅替换网络，避免伪造页面加载状态。 */
import { fireEvent, waitFor } from '@testing-library/vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { userSettings } from '@/test-support/actionFixtures'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import LoginPage from './LoginPage.vue'

/** 认证 fixture 沿用共享设置中的时区与简报时间，身份完全合成，不建立真实会话。 */
const user = {
  ...userSettings(),
  id: 'login-test-user',
  email: 'admin@example.com',
  display_name: '测试管理员',
}
const rejected = {
  type: 'about:blank',
  title: 'Invalid credentials',
  status: 401,
  detail: 'The email or password was not accepted.',
  instance: '',
  error_code: 'invalid_credentials',
  trace_id: 'login-test-trace',
}

/**
 * 填写合成凭据并通过具名表单提交；不依赖任何组件内部结构。
 * @param view 已安装真实认证 Store 的页面渲染结果。
 * @returns 本次提交事件及 Vue 渲染完成后结束，网络结果由各测试控制。
 */
async function fillAndSubmit(
  view: Awaited<ReturnType<typeof renderWithPlugins>>,
): Promise<void> {
  await fireEvent.update(view.getByLabelText('邮箱'), user.email)
  await fireEvent.update(view.getByLabelText('密码'), 'synthetic-test-password')
  await fireEvent.submit(view.getByRole('form'))
}

afterEach(() => vi.unstubAllGlobals())

describe('LoginPage', () => {
  it('names its form from the visible heading', async () => {
    const view = await renderWithPlugins(LoginPage)
    expect(
      view.getByRole('form', { name: '登录 AI Employee' }),
    ).toHaveAttribute('aria-labelledby', 'login-title')
  })

  it('toggles password visibility with an explicit pressed state', async () => {
    const view = await renderWithPlugins(LoginPage)
    const toggle = view.getByRole('button', { name: '显示密码' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await fireEvent.click(toggle)
    expect(view.getByLabelText('密码')).toHaveAttribute('type', 'text')
    expect(view.getByRole('button', { name: '显示密码' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await fireEvent.click(view.getByRole('button', { name: '显示密码' }))
    expect(view.getByLabelText('密码')).toHaveAttribute('type', 'password')
    expect(view.queryByRole('dialog')).toBeNull()
  })

  it('labels credentials and disables submission while authentication is pending', async () => {
    let resolveResponse: (value: Response) => void = () => {
      throw new Error('请求尚未建立')
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveResponse = resolve
          }),
      ),
    )
    const view = await renderWithPlugins(LoginPage)
    expect(view.getByLabelText('邮箱')).toHaveAttribute(
      'autocomplete',
      'username',
    )
    expect(view.getByLabelText('密码')).toHaveAttribute('type', 'password')
    expect(view.getByLabelText('密码')).toHaveAttribute(
      'autocomplete',
      'current-password',
    )
    await fillAndSubmit(view)
    expect(view.getByRole('button', { name: '登录中…' })).toBeDisabled()
    resolveResponse(new Response(JSON.stringify(user)))
    await waitFor(() =>
      expect(view.router.currentRoute.value.path).toBe('/tasks'),
    )
    expect(view.getByLabelText('密码')).toHaveValue('')
  })

  it('preserves the actual 401 title in one alert and retains email for correction', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () => new Response(JSON.stringify(rejected), { status: 401 }),
      ),
    )
    const view = await renderWithPlugins(LoginPage)
    await fillAndSubmit(view)
    expect(await view.findByRole('alert')).toHaveTextContent(
      'Invalid credentials',
    )
    expect(view.getAllByRole('alert')).toHaveLength(1)
    expect(view.getByRole('alert')).toHaveTextContent(
      '请检查邮箱和密码后重试。',
    )
    expect(view.getByLabelText('邮箱')).toHaveValue(user.email)
    expect(view.getByRole('button', { name: '登录' })).toBeEnabled()
  })

  it('preserves the recoverable network error message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('synthetic network failure')
      }),
    )
    const view = await renderWithPlugins(LoginPage)
    await fillAndSubmit(view)
    expect(await view.findByRole('alert')).toHaveTextContent(
      '登录暂时不可用，请稍后重试。',
    )
  })

  it.each([
    ['/brief', '/brief'],
    ['//example.com', '/tasks'],
    ['https://example.com', '/tasks'],
  ])(
    'handles redirect %s as %s without leaving the application',
    async (redirect, destination) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => new Response(JSON.stringify(user))),
      )
      const view = await renderWithPlugins(LoginPage, {
        route: `/login?redirect=${encodeURIComponent(redirect)}`,
      })
      await fillAndSubmit(view)
      await waitFor(() =>
        expect(view.router.currentRoute.value.path).toBe(destination),
      )
    },
  )
})

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'
import * as api from '@/api/connections'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import {
  connection,
  connectionCapabilities,
} from '@/test-support/editorFixtures'
import { useConnections } from './useConnections'

vi.mock('@/api/connections', async (original) => ({
  ...(await original<typeof import('@/api/connections')>()),
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
  enableConnectionCapability: vi.fn(),
  disableConnectionCapability: vi.fn(),
  disconnectConnection: vi.fn(),
  startGoogleConnection: vi.fn(),
  startMicrosoftConnection: vi.fn(),
  syncConnection: vi.fn(),
}))

/** 由测试显式控制确认及网络边界，不依赖定时器或真实账户。 */
function deferred<T>() {
  let resolve: (value: T) => void = () => {
    throw new Error('deferred not initialized')
  }
  let reject: (reason: unknown) => void = () => {
    throw new Error('deferred not initialized')
  }
  const promise = new Promise<T>((accept, refuse) => {
    resolve = accept
    reject = refuse
  })
  return { promise, resolve, reject }
}

/** 排空已就绪的 API 与 Vue 微任务；等待始终由本测试持有的 Promise 决定。 */
async function flush(): Promise<void> {
  for (let index = 0; index < 20; index += 1) await Promise.resolve()
  await nextTick()
}

let state: ReturnType<typeof useConnections>

/** 真实挂载 composable，只替换其外部 API，保留目录加载与卸载钩子。 */
async function mount(
  confirmDisconnect?: (message: string) => boolean | Promise<boolean>,
) {
  const probe = defineComponent({
    setup() {
      state = confirmDisconnect
        ? useConnections({ confirmDisconnect })
        : useConnections()
      return () => h('output', state.busy.value ? '处理中' : '就绪')
    },
  })
  const view = await renderWithPlugins(probe)
  await flush()
  return view
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  vi.mocked(api.listConnections).mockResolvedValue([connection()])
  vi.mocked(api.getConnectionCapabilities).mockResolvedValue(
    connectionCapabilities(),
  )
  vi.mocked(api.disconnectConnection).mockResolvedValue(null)
  vi.mocked(api.enableConnectionCapability).mockResolvedValue({
    authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth',
    requested_capabilities: ['mail.read', 'mail.send'],
  })
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('连接断开的异步确认边界', () => {
  it('确认未完成时互斥所有动作，取消零请求且保留原授权提示', async () => {
    const decision = deferred<boolean>()
    const confirm = vi.fn(() => decision.promise)
    await mount(confirm)
    await state.enable(connection(), 'mail.send')
    const notice = state.notice.value
    const authorization = state.authorization.value
    const initialReads = vi.mocked(api.listConnections).mock.calls.length

    const pending = state.disconnect(connection())
    expect(state.busy.value).toBe(true)
    await state.disconnect(connection())
    await state.connect('google')
    await state.sync(connection())
    await state.disable(connection(), 'mail.send')
    expect(confirm).toHaveBeenCalledExactlyOnceWith(
      '确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。',
    )
    expect(window.confirm).not.toHaveBeenCalled()
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    expect(api.startGoogleConnection).not.toHaveBeenCalled()
    expect(api.syncConnection).not.toHaveBeenCalled()
    expect(api.disableConnectionCapability).not.toHaveBeenCalled()
    decision.resolve(false)
    await pending
    expect(state.busy.value).toBe(false)
    expect(state.notice.value).toBe(notice)
    expect(state.authorization.value).toEqual(authorization)
    expect(api.listConnections).toHaveBeenCalledTimes(initialReads)
  })

  it('确认后仅断开精确连接一次，直到请求及目录刷新完成才解除互斥', async () => {
    const decision = deferred<boolean>()
    const request = deferred<null>()
    const refresh = deferred<ReturnType<typeof connection>[]>()
    const confirm = vi.fn(() => decision.promise)
    vi.mocked(api.disconnectConnection).mockReturnValue(request.promise)
    await mount(confirm)
    vi.mocked(api.listConnections).mockReturnValueOnce(refresh.promise)
    const pending = state.disconnect(connection())
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    decision.resolve(true)
    await flush()
    expect(api.disconnectConnection).toHaveBeenCalledExactlyOnceWith(
      connection().id,
    )
    expect(state.busy.value).toBe(true)
    await state.disconnect(connection())
    request.resolve(null)
    await flush()
    expect(state.busy.value).toBe(true)
    expect(api.listConnections).toHaveBeenCalledTimes(2)
    refresh.resolve([connection()])
    await pending
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(state.busy.value).toBe(false)
  })

  it.each([false, true])(
    '默认原生确认结果为 %s 时保留原断开请求语义',
    async (accepted) => {
      vi.mocked(window.confirm).mockReturnValue(accepted)
      await mount()
      await state.disconnect(connection())
      expect(window.confirm).toHaveBeenCalledExactlyOnceWith(
        '确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。',
      )
      expect(api.disconnectConnection).toHaveBeenCalledTimes(accepted ? 1 : 0)
      if (accepted)
        expect(api.disconnectConnection).toHaveBeenCalledWith(connection().id)
      expect(api.listConnections).toHaveBeenCalledTimes(accepted ? 2 : 1)
      expect(state.busy.value).toBe(false)
    },
  )

  it('确认组件失败时恢复互斥并给出安全错误，不发断开请求', async () => {
    const decision = deferred<boolean>()
    // 旧实现不消费注入回调时，也让 RED 只来自业务断言；传入的原 Promise 仍保持拒绝。
    void decision.promise.catch(() => undefined)
    await mount(() => decision.promise)
    const pending = state.disconnect(connection())
    decision.reject(new Error('synthetic private UI detail'))
    await pending
    expect(state.actionError.value).toEqual({
      message: '请求失败，请重试。',
      traceId: null,
      action: 'retry',
    })
    expect(state.busy.value).toBe(false)
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    expect(api.listConnections).toHaveBeenCalledTimes(1)
  })

  it('确认后的断开失败仍按原恢复规则展示错误，不伪造目录刷新成功', async () => {
    vi.mocked(api.disconnectConnection).mockRejectedValue(
      new Error('synthetic private network detail'),
    )
    await mount(() => Promise.resolve(true))
    await state.disconnect(connection())
    expect(api.disconnectConnection).toHaveBeenCalledExactlyOnceWith(
      connection().id,
    )
    expect(api.listConnections).toHaveBeenCalledTimes(1)
    expect(state.actionError.value).toEqual({
      message: '请求失败，请重试。',
      traceId: null,
      action: 'retry',
    })
    expect(state.busy.value).toBe(false)
  })

  it('等待确认时卸载，迟到的同意不得发送断开或刷新', async () => {
    const decision = deferred<boolean>()
    const view = await mount(() => decision.promise)
    const pending = state.disconnect(connection())
    view.unmount()
    decision.resolve(true)
    await pending
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    expect(api.listConnections).toHaveBeenCalledTimes(1)
  })

  it('断开请求期间卸载，已发请求落定后不得刷新目录', async () => {
    const request = deferred<null>()
    vi.mocked(api.disconnectConnection).mockReturnValue(request.promise)
    const view = await mount(() => Promise.resolve(true))
    const pending = state.disconnect(connection())
    await flush()
    expect(api.disconnectConnection).toHaveBeenCalledExactlyOnceWith(
      connection().id,
    )
    view.unmount()
    request.resolve(null)
    await pending
    expect(api.listConnections).toHaveBeenCalledTimes(1)
  })
})

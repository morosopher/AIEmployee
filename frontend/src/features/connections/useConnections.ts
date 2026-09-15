import { onMounted, onUnmounted, ref } from 'vue'
import {
  disableConnectionCapability,
  disconnectConnection,
  enableConnectionCapability,
  startGoogleConnection,
  startMicrosoftConnection,
  syncConnection,
} from '@/api/connections'
import type { ActionProvider, CapabilityName, Connection } from '@/api/types'
import { useConnectionCatalog } from '@/composables/useConnectionCatalog'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'

/**
 * 连接页面的受控行为。授权链接只来自已验证 API，不持久化 OAuth 响应或自动换账户。
 * @returns 目录、互斥请求状态、明确授权链接及固定能力动作。
 */
export function useConnections() {
  const catalog = useConnectionCatalog()
  const busy = ref(false),
    notice = ref('')
  const error = ref<ActionRecovery | null>(null)
  const authorization = ref<{ url: string; provider: ActionProvider } | null>(
    null,
  )
  let disposed = false
  onUnmounted(() => {
    disposed = true
  })
  onMounted(() => void catalog.load())

  /** 包含已授权短事务的请求期间防止重复点击；失败不伪装为连接完成。 */
  async function perform(action: () => Promise<void>): Promise<void> {
    if (busy.value) return
    busy.value = true
    error.value = null
    notice.value = ''
    authorization.value = null
    try {
      await action()
    } catch (cause) {
      if (!disposed) error.value = actionRecovery(cause)
    } finally {
      if (!disposed) busy.value = false
    }
  }
  /** @param provider 用户明确选择的供应商。只启动读取授权，写能力另行申请。 */
  function connect(provider: ActionProvider): Promise<void> {
    return perform(async () => {
      const result =
        provider === 'google'
          ? await startGoogleConnection()
          : await startMicrosoftConnection()
      if (!disposed)
        authorization.value = { url: result.authorization_url, provider }
    })
  }
  /** @param connection 精确账户。@param capability 用户选择的单项能力及其服务端依赖闭包。 */
  function enable(
    connection: Connection,
    capability: CapabilityName,
  ): Promise<void> {
    return perform(async () => {
      const result = await enableConnectionCapability(
        connection.id,
        capability,
        connection.provider,
      )
      if (!disposed) {
        authorization.value = {
          url: result.authorization_url,
          provider: connection.provider,
        }
        notice.value = `本次授权包含：${result.requested_capabilities.join('、')}。请继续授权，完成后将返回连接页。`
        // 发起会改变整个依赖闭包的状态；只读取服务端事实，不把 URL 的生成解释为启用成功。
        await catalog.load()
      }
    })
  }
  /** @param connection 精确账户。@param capability 本地关闭项；依赖冲突仍由后端拒绝。 */
  function disable(
    connection: Connection,
    capability: CapabilityName,
  ): Promise<void> {
    return perform(async () => {
      await disableConnectionCapability(connection.id, capability)
      if (!disposed) await catalog.load()
    })
  }
  /** @param connection 明确用户账户。同步 receipt 只表示排队，不表示数据完整。 */
  function sync(connection: Connection): Promise<void> {
    return perform(async () => {
      await syncConnection(connection.id)
      if (!disposed) notice.value = '同步已排队，数据完整性以任务结果为准。'
    })
  }
  /** @param connection 待断开账户。保留既有明确确认，绝不暗示远端写入已撤销。 */
  function disconnect(connection: Connection): Promise<void> {
    if (
      !window.confirm(
        '确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。',
      )
    )
      return Promise.resolve()
    return perform(async () => {
      await disconnectConnection(connection.id)
      if (!disposed) await catalog.load()
    })
  }
  return {
    ...catalog,
    busy,
    notice,
    actionError: error,
    authorization,
    connect,
    enable,
    disable,
    sync,
    disconnect,
  }
}

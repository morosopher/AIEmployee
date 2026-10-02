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
 * 仅提供展示层确认结果；实际断开请求仍由本 composable 在确认后发送。
 * @param message 完整断开警告，不含账户凭据或远端正文。
 * @returns 同意为 true，取消为 false；可异步等待 Dialog，关闭或卸载应结算为取消。
 */
export type ConfirmConnectionDisconnect = (
  message: string,
) => boolean | Promise<boolean>

/**
 * 连接页面的受控行为。授权链接只来自已验证 API，不持久化 OAuth 响应或自动换账户。
 * @param options.confirmDisconnect 可替换的 UI 确认端口；缺省保留原生确认及原警告。
 * @returns 目录、互斥请求状态、明确授权链接及固定能力动作。
 */
export function useConnections({
  confirmDisconnect = (message) => window.confirm(message),
}: { confirmDisconnect?: ConfirmConnectionDisconnect } = {}) {
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
  /**
   * 等待 UI 的精确确认后断开账户；取消保留当前授权链接、通知和错误，不发送请求。
   * @param connection 待断开账户。确认不能撤销已经执行的远端写入。
   * @returns 确认、断开和既有目录刷新全部落定后结束；卸载后不发后续请求或写状态。
   */
  async function disconnect(connection: Connection): Promise<void> {
    if (busy.value || disposed) return
    busy.value = true
    let accepted = false
    try {
      accepted = await confirmDisconnect(
        '确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。',
      )
    } catch (cause) {
      if (!disposed) error.value = actionRecovery(cause)
    } finally {
      if (!disposed) busy.value = false
    }
    if (!accepted || disposed) return
    // 不在确认取消时清空原提示；同一同步续段立即由 perform 接管锁，没有可交互空隙。
    await perform(async () => {
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

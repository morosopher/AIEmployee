import { onUnmounted, ref } from 'vue'
import { getConnectionCapabilities, listConnections } from '@/api/connections'
import type {
  CapabilityName,
  Connection,
  ConnectionCapabilities,
} from '@/api/types'
import {
  actionRecovery,
  type ActionRecovery,
} from '@/features/actions/recovery'

/** 一条连接及独立能力读取状态；部分失败不抹掉其他账户的已验证目录。 */
export interface ConnectionCatalogEntry {
  connection: Connection
  capabilities: ConnectionCapabilities | null
  error: ActionRecovery | null
}

/**
 * 加载受控账户选择目录，只保存页面内存中的 API 事实。
 * @returns 明确加载/部分错误状态及手动刷新函数；不自动选择默认账户。
 */
export function useConnectionCatalog() {
  const entries = ref<ConnectionCatalogEntry[]>([])
  const loading = ref(false),
    loaded = ref(false)
  const error = ref<ActionRecovery | null>(null)
  let generation = 0
  let disposed = false
  onUnmounted(() => {
    disposed = true
    generation += 1
  })

  /** 并行读取互相独立的能力；迟到响应不得写回已卸载或已刷新的选择器。 */
  async function load(): Promise<void> {
    const owner = ++generation
    loading.value = true
    error.value = null
    try {
      const connections = await listConnections()
      const results = await Promise.allSettled(
        connections.map((connection) =>
          getConnectionCapabilities(connection.id),
        ),
      )
      if (disposed || owner !== generation) return
      entries.value = connections.map((connection, index) => {
        const result = results[index]
        return result?.status === 'fulfilled'
          ? { connection, capabilities: result.value, error: null }
          : {
              connection,
              capabilities: null,
              error: actionRecovery(
                result?.status === 'rejected' ? result.reason : null,
              ),
            }
      })
      loaded.value = true
    } catch (cause) {
      if (!disposed && owner === generation) error.value = actionRecovery(cause)
    } finally {
      if (!disposed && owner === generation) loading.value = false
    }
  }
  return { entries, loading, loaded, error, load }
}

/**
 * @param entry 服务端目录项。
 * @param capability 所需能力；写能力同时要求对应读取状态启用。
 * @returns UI 选择是否可用；服务端仍会在创建、保存和冻结边界重新授权。
 */
export function hasCapability(
  entry: ConnectionCatalogEntry,
  capability: CapabilityName,
): boolean {
  if (entry.connection.status !== 'connected') return false
  const required: CapabilityName[] =
    capability === 'mail.send'
      ? ['mail.read', 'mail.send']
      : capability === 'calendar.write'
        ? ['calendar.read', 'calendar.write']
        : [capability]
  return required.every((name) =>
    entry.capabilities?.capabilities.some(
      (row) => row.capability === name && row.status === 'enabled',
    ),
  )
}

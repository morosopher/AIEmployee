import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useActionsStore } from './actions'
import {
  actionItems,
  actionSnapshot,
  NOW,
  TASK_ID,
} from '@/test-support/actionFixtures'
import type {
  ActionListItem,
  ActionSnapshot,
  TaskEvent,
  TaskStatus,
} from '@/api/types'

/** 创建只含状态与尝试次数的持久事件。 */
function event(
  sequence: string,
  name = 'task.status_changed',
  payload: TaskEvent['payload'] = { status: 'reconciling' },
): TaskEvent {
  return {
    id: sequence,
    sequence,
    task_id: TASK_ID,
    event: name,
    occurred_at: NOW,
    step_id: null,
    payload,
  }
}

/** @returns 状态一致的合成执行中快照，避免解析失败掩盖列表与详情的读取竞争。 */
function runningSnapshot(): ActionSnapshot {
  const snapshot = actionSnapshot()
  return {
    ...snapshot,
    status: 'running',
    error_code: null,
    timeline: [],
    local_action: snapshot.local_action
      ? { ...snapshot.local_action, status: 'executing' }
      : null,
    execution: snapshot.execution
      ? { ...snapshot.execution, status: 'executing' }
      : null,
  }
}

/** @param status 服务端明确返回的任务状态。 @returns 合法的合成任务列表行。 */
function taskItem(status: TaskStatus): ActionListItem {
  const item = actionItems().find((entry) => entry.item_kind === 'trusted_task')
  if (!item || item.item_kind !== 'trusted_task')
    throw new Error('Missing synthetic task row')
  return { ...item, status }
}

beforeEach(() => setActivePinia(createPinia()))
afterEach(() => vi.unstubAllGlobals())

describe('server authoritative action state', () => {
  it('replaces list snapshots without inventing task IDs for local editing objects', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: actionItems(), limit: 50, offset: 0 }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], limit: 50, offset: 0 })),
      )
    vi.stubGlobal('fetch', fetcher)
    const store = useActionsStore()
    await store.refreshList()
    expect(store.items[0]?.task_id).toBeNull()
    expect(store.items).toHaveLength(3)
    await store.refreshList()
    expect(store.items).toHaveLength(0)
  })

  it('keeps a newer REST snapshot when a list started before it arrives late', async () => {
    let listReads = 0
    let snapshotReads = 0
    let finishOldList: (response: Response) => void = () => undefined
    const oldPage = { items: [taskItem('running')], limit: 50, offset: 0 }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/v1/actions') {
          listReads += 1
          if (listReads === 2)
            return new Promise<Response>((resolve) => {
              finishOldList = resolve
            })
          return new Response(
            JSON.stringify(
              listReads === 1
                ? oldPage
                : {
                    items: [taskItem('needs_attention')],
                    limit: 50,
                    offset: 0,
                  },
            ),
          )
        }
        snapshotReads += 1
        return new Response(
          JSON.stringify(
            snapshotReads === 1
              ? runningSnapshot()
              : actionSnapshot({
                  event_cursor: '9007199254740994',
                  task_version: '9007199254740994',
                }),
          ),
        )
      }),
    )
    const store = useActionsStore()
    await store.refreshList()
    await store.loadSnapshot(TASK_ID)
    // focus/reconnect 同时发出两类 GET；旧列表必须等新详情已被采纳后才返回。
    const pendingList = store.refreshList()
    await store.loadSnapshot(TASK_ID)
    expect(store.items[0]?.status).toBe('needs_attention')
    finishOldList(new Response(JSON.stringify(oldPage)))
    await pendingList
    expect(store.snapshots[TASK_ID]?.status).toBe('needs_attention')
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740994')
    expect(store.items[0]?.status).toBe('needs_attention')
    expect(listReads).toBe(3)
    expect(store.listError).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('rereads the same filtered page when a newer snapshot moves a task out of it', async () => {
    const urls: string[] = []
    let snapshotReads = 0
    let finishOldList: (response: Response) => void = () => undefined
    const oldPage = { items: [taskItem('running')], limit: 1, offset: 1 }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.startsWith('/api/v1/actions?')) {
          urls.push(url)
          if (urls.length === 2)
            return new Promise<Response>((resolve) => {
              finishOldList = resolve
            })
          return new Response(
            JSON.stringify(
              urls.length === 1
                ? oldPage
                : {
                    items: [],
                    limit: 1,
                    offset: 1,
                  },
            ),
          )
        }
        snapshotReads += 1
        return new Response(
          JSON.stringify(
            snapshotReads === 1
              ? runningSnapshot()
              : actionSnapshot({
                  event_cursor: '9007199254740994',
                  task_version: '9007199254740994',
                }),
          ),
        )
      }),
    )
    const store = useActionsStore()
    await store.refreshList({
      provider: 'google',
      status: 'running',
      limit: 1,
      offset: 1,
    })
    await store.loadSnapshot(TASK_ID)
    const pendingList = store.refreshList()
    await store.loadSnapshot(TASK_ID)
    finishOldList(new Response(JSON.stringify(oldPage)))
    await pendingList
    // 服务端已把该任务移出 running 的第二页，缓存详情不能把它重新插入或覆盖筛选。
    expect(store.items).toHaveLength(0)
    expect(store.snapshots[TASK_ID]?.status).toBe('needs_attention')
    expect(store.limit).toBe(1)
    expect(store.offset).toBe(1)
    expect(store.filters).toEqual({
      provider: 'google',
      status: 'running',
      limit: 1,
      offset: 1,
    })
    expect(urls).toEqual([
      '/api/v1/actions?provider=google&status=running&limit=1&offset=1',
      '/api/v1/actions?provider=google&status=running&limit=1&offset=1',
      '/api/v1/actions?provider=google&status=running&limit=1&offset=1',
    ])
  })

  it.each([
    { name: 'cached cursor', cached: true, lateCursor: '9007199254740993' },
    { name: 'first detail', cached: false, lateCursor: '9007199254740993' },
    { name: 'higher cursor', cached: true, lateCursor: '9007199254740994' },
  ])(
    'rereads a late detail after a newer list is accepted ($name)',
    async ({ cached, lateCursor }) => {
      let listReads = 0
      let snapshotReads = 0
      let finishOldDetail: (response: Response) => void = () => undefined
      let finishRecovery: (response: Response) => void = () => undefined
      const delayedRead = cached ? 2 : 1
      const newerTime = '2030-01-01T00:01:00Z'
      const newerItem = {
        ...taskItem('needs_attention'),
        updated_at: newerTime,
      }
      const lateSnapshot = {
        ...runningSnapshot(),
        event_cursor: lateCursor,
        task_version: lateCursor,
      }
      const recoveredSnapshot = actionSnapshot({
        event_cursor: '9007199254740995',
        task_version: '9007199254740995',
        updated_at: newerTime,
      })
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string) => {
          if (url === '/api/v1/actions') {
            listReads += 1
            return new Response(
              JSON.stringify({ items: [newerItem], limit: 50, offset: 0 }),
            )
          }
          snapshotReads += 1
          if (snapshotReads === delayedRead)
            return new Promise<Response>((resolve) => {
              finishOldDetail = resolve
            })
          if (snapshotReads === delayedRead + 1)
            return new Promise<Response>((resolve) => {
              finishRecovery = resolve
            })
          if (cached && snapshotReads === 1)
            return new Response(JSON.stringify(runningSnapshot()))
          throw new Error('Unexpected synthetic detail read')
        }),
      )
      const store = useActionsStore()
      if (cached) await store.loadSnapshot(TASK_ID)
      const previousSnapshot = store.snapshots[TASK_ID]
      const pending = store.loadSnapshot(TASK_ID)
      const duplicate = store.loadSnapshot(TASK_ID)
      await store.refreshList()
      expect(store.items[0]?.status).toBe('needs_attention')
      expect(snapshotReads).toBe(delayedRead)

      // 完整后续响应在 RED 时已定义：旧 GET 无论是否高于缓存，都不能覆盖后来采纳的列表。
      finishOldDetail(new Response(JSON.stringify(lateSnapshot)))
      await flushPromises()
      expect(store.items[0]?.status).toBe('needs_attention')
      expect(store.items[0]?.updated_at).toBe(newerTime)
      expect(store.snapshots[TASK_ID]).toBe(previousSnapshot)
      expect(store.snapshotErrors[TASK_ID]).toBeNull()
      expect(store.snapshotLoading[TASK_ID]).toBe(true)
      expect(snapshotReads).toBe(delayedRead + 1)

      // 补读晚于列表采纳启动，返回新的权威快照；同任务并发调用共用这一次恢复。
      finishRecovery(new Response(JSON.stringify(recoveredSnapshot)))
      await Promise.all([pending, duplicate])
      expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740995')
      expect(store.snapshots[TASK_ID]?.status).toBe('needs_attention')
      expect(store.items[0]?.status).toBe('needs_attention')
      expect(store.snapshotErrors[TASK_ID]).toBeNull()
      expect(store.snapshotLoading[TASK_ID]).toBe(false)
      expect(snapshotReads).toBe(delayedRead + 1)
      expect(listReads).toBe(1)
    },
  )

  it('rereads a later-started list when the earlier detail completes first', async () => {
    let listReads = 0
    let snapshotReads = 0
    let finishDetail: (response: Response) => void = () => undefined
    let finishList: (response: Response) => void = () => undefined
    const newerSnapshot = actionSnapshot({
      event_cursor: '9007199254740995',
      task_version: '9007199254740995',
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/v1/actions') {
          listReads += 1
          if (listReads === 1)
            return new Promise<Response>((resolve) => {
              finishList = resolve
            })
          return new Response(
            JSON.stringify({
              items: [taskItem('needs_attention')],
              limit: 50,
              offset: 0,
            }),
          )
        }
        snapshotReads += 1
        if (snapshotReads === 2)
          return new Promise<Response>((resolve) => {
            finishDetail = resolve
          })
        return new Response(JSON.stringify(runningSnapshot()))
      }),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    const detail = store.loadSnapshot(TASK_ID)
    const list = store.refreshList()
    // 交换同一次竞争的完成顺序；详情被采纳后，列表只补读一次，不相互触发无限刷新。
    finishDetail(new Response(JSON.stringify(newerSnapshot)))
    await detail
    expect(store.snapshots[TASK_ID]?.status).toBe('needs_attention')
    finishList(
      new Response(
        JSON.stringify({ items: [taskItem('running')], limit: 50, offset: 0 }),
      ),
    )
    await list
    expect(store.items[0]?.status).toBe('needs_attention')
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740995')
    expect(store.snapshotErrors[TASK_ID]).toBeNull()
    expect(store.listError).toBeNull()
    expect(snapshotReads).toBe(2)
    expect(listReads).toBe(2)
  })

  it('accepts an authoritative detail requested after a newer list was adopted', async () => {
    let snapshotReads = 0
    let listReads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/v1/actions') {
          listReads += 1
          return new Response(
            JSON.stringify({
              items: [taskItem('needs_attention')],
              limit: 50,
              offset: 0,
            }),
          )
        }
        snapshotReads += 1
        return new Response(
          JSON.stringify(
            snapshotReads === 1
              ? runningSnapshot()
              : actionSnapshot({
                  status: 'succeeded',
                  event_cursor: '9007199254740995',
                  task_version: '9007199254740995',
                }),
          ),
        )
      }),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    await store.refreshList()
    expect(store.items[0]?.status).toBe('needs_attention')
    await store.loadSnapshot(TASK_ID)
    expect(store.items[0]?.status).toBe('succeeded')
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740995')
    expect(store.snapshotErrors[TASK_ID]).toBeNull()
    expect(snapshotReads).toBe(2)
    expect(listReads).toBe(1)
  })

  it('bounds detail recovery when another list is adopted during the reread', async () => {
    let listReads = 0
    let snapshotReads = 0
    let finishOldDetail: (response: Response) => void = () => undefined
    let finishRecovery: (response: Response) => void = () => undefined
    const firstNewTime = '2030-01-01T00:01:00Z'
    const latestTime = '2030-01-01T00:02:00Z'
    const interruptedRecovery = actionSnapshot({
      event_cursor: '9007199254740995',
      task_version: '9007199254740995',
      updated_at: firstNewTime,
    })
    const finalSnapshot = actionSnapshot({
      status: 'succeeded',
      event_cursor: '9007199254740996',
      task_version: '9007199254740996',
      updated_at: latestTime,
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/v1/actions') {
          listReads += 1
          return new Response(
            JSON.stringify({
              items: [
                {
                  ...taskItem(
                    listReads === 1 ? 'needs_attention' : 'succeeded',
                  ),
                  updated_at: listReads === 1 ? firstNewTime : latestTime,
                },
              ],
              limit: 50,
              offset: 0,
            }),
          )
        }
        snapshotReads += 1
        if (snapshotReads === 2)
          return new Promise<Response>((resolve) => {
            finishOldDetail = resolve
          })
        if (snapshotReads === 3)
          return new Promise<Response>((resolve) => {
            finishRecovery = resolve
          })
        if (snapshotReads === 1 || snapshotReads === 4)
          return new Response(
            JSON.stringify(
              snapshotReads === 1 ? runningSnapshot() : finalSnapshot,
            ),
          )
        throw new Error('Unexpected synthetic detail read')
      }),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    const previousSnapshot = store.snapshots[TASK_ID]
    const detail = store.loadSnapshot(TASK_ID)
    await store.refreshList()
    finishOldDetail(new Response(JSON.stringify(runningSnapshot())))
    await flushPromises()
    expect(store.items[0]?.status).toBe('needs_attention')
    expect(snapshotReads).toBe(3)

    // 第二次列表采纳使补读也无法证明新鲜；本次调用必须结束，不能靠不停 GET 追赶列表。
    await store.refreshList()
    finishRecovery(new Response(JSON.stringify(interruptedRecovery)))
    await detail
    expect(store.items[0]?.status).toBe('succeeded')
    expect(store.items[0]?.updated_at).toBe(latestTime)
    expect(store.snapshots[TASK_ID]).toBe(previousSnapshot)
    expect(store.snapshotErrors[TASK_ID]).toEqual({
      message: '无法刷新操作，请重试。',
      trace_id: null,
    })
    expect(store.snapshotLoading[TASK_ID]).toBe(false)
    expect(snapshotReads).toBe(3)
    expect(listReads).toBe(2)

    // 用户后续显式重试取得真正后发的服务端结果，不受上一次已耗尽预算的永久影响。
    await store.loadSnapshot(TASK_ID)
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740996')
    expect(store.items[0]?.status).toBe('succeeded')
    expect(store.snapshotErrors[TASK_ID]).toBeNull()
    expect(snapshotReads).toBe(4)
    expect(listReads).toBe(2)
  })

  it('accepts a later server list without overlaying an older cached snapshot', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url === '/api/v1/actions'
                ? { items: [taskItem('succeeded')], limit: 50, offset: 0 }
                : actionSnapshot(),
            ),
          ),
      ),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    await store.refreshList()
    expect(store.items[0]?.status).toBe('succeeded')
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740993')
  })

  it('discards an invalidated old-owner list without refreshing the new owner', async () => {
    let listReads = 0
    let finishOldList: (response: Response) => void = () => undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === `/api/v1/actions/${TASK_ID}`)
          return new Response(JSON.stringify(actionSnapshot()))
        listReads += 1
        if (listReads === 1)
          return new Promise<Response>((resolve) => {
            finishOldList = resolve
          })
        return new Response(
          JSON.stringify({
            items: actionItems().filter(
              (item) => item.item_kind === 'calendar_proposal',
            ),
            limit: 50,
            offset: 0,
          }),
        )
      }),
    )
    const store = useActionsStore()
    const pendingList = store.refreshList()
    await store.loadSnapshot(TASK_ID)
    store.clear()
    await store.refreshList({ item_kind: 'calendar_proposal' })
    finishOldList(
      new Response(
        JSON.stringify({ items: [taskItem('running')], limit: 50, offset: 0 }),
      ),
    )
    await pendingList
    expect(listReads).toBe(2)
    expect(store.items).toHaveLength(1)
    expect(store.items[0]?.item_kind).toBe('calendar_proposal')
    expect(store.filters).toEqual({ item_kind: 'calendar_proposal' })
    expect(Object.keys(store.snapshots)).toHaveLength(0)
  })

  it('deduplicates and orders events above the safe integer limit without stale rollback', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify(actionSnapshot()))),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    store.applyEvent(event('9007199254740995'))
    store.applyEvent(
      event('9007199254740994', 'task.status_changed', { status: 'running' }),
    )
    store.applyEvent(
      event('9007199254740995', 'task.status_changed', { status: 'failed' }),
    )
    expect(store.snapshots[TASK_ID]?.status).toBe('reconciling')
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740993')
  })

  it('refetches needs-attention and terminal transitions and ignores unknown payload fields', async () => {
    const fetcher = vi.fn().mockImplementation(
      async () =>
        new Response(
          JSON.stringify(
            actionSnapshot({
              event_cursor: '9007199254740995',
              task_version: '9007199254740995',
              status: 'needs_attention',
            }),
          ),
        ),
    )
    vi.stubGlobal('fetch', fetcher)
    const store = useActionsStore()
    store.applyEvent(
      event('9007199254740994', 'tool.oauth_refresh_confirmed', {
        status: 'retryable_failed',
        body_text: 'not-rendered',
      }),
    )
    await flushPromises()
    expect(store.snapshots[TASK_ID]?.status).toBe('needs_attention')
    expect('body_text' in (store.snapshots[TASK_ID] ?? {})).toBe(false)
    fetcher.mockImplementation(
      async () =>
        new Response(
          JSON.stringify(
            actionSnapshot({
              event_cursor: '9007199254740997',
              task_version: '9007199254740997',
              status: 'succeeded',
              reconciliation_attempt_count: 3,
            }),
          ),
        ),
    )
    store.applyEvent(
      event('9007199254740996', 'task.status_changed', { status: 'succeeded' }),
    )
    await flushPromises()
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740997')
    expect(store.snapshots[TASK_ID]?.reconciliation_attempt_count).toBe(3)
    expect(store.snapshots[TASK_ID]?.status).toBe('succeeded')
  })

  it('recovers an unknown event with a REST snapshot rather than deriving a result', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(actionSnapshot()))),
    )
    const store = useActionsStore()
    store.applyEvent(
      event('9007199254740993', 'future.event', { status: 'succeeded' }),
    )
    await flushPromises()
    expect(store.snapshots[TASK_ID]?.status).toBe('needs_attention')
  })

  it('sends the latest server task_version for manual confirmation and reloads the minimal response', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    let resolved = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init })
        if (url.endsWith('/manual-resolution')) {
          resolved = true
          return new Response(
            JSON.stringify({
              task_id: TASK_ID,
              task_version: '9007199254740994',
            }),
          )
        }
        return new Response(
          JSON.stringify(
            actionSnapshot(
              resolved
                ? {
                    status: 'failed',
                    event_cursor: '9007199254740994',
                    task_version: '9007199254740994',
                  }
                : {},
            ),
          ),
        )
      }),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    await store.resolveManually(TASK_ID, 'confirmed_not_executed')
    const mutation = calls.find((call) =>
      call.url.endsWith('/manual-resolution'),
    )
    expect(mutation).toBeDefined()
    expect(JSON.parse(String(mutation?.init.body))).toEqual({
      resolution: 'confirmed_not_executed',
      task_version: '9007199254740993',
    })
    expect(store.snapshots[TASK_ID]?.status).toBe('failed')
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740994')
  })

  it('refetches a mutation version after an older background snapshot was already in flight', async () => {
    let finishMutation: (response: Response) => void = () => undefined
    let finishBackground: (response: Response) => void = () => undefined
    let reads = 0
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith('/manual-resolution'))
        return new Promise<Response>((resolve) => {
          finishMutation = resolve
        })
      reads += 1
      if (reads === 3)
        return new Promise<Response>((resolve) => {
          finishBackground = resolve
        })
      return new Response(
        JSON.stringify(
          actionSnapshot(
            reads >= 4
              ? {
                  status: 'failed',
                  event_cursor: '9007199254740994',
                  task_version: '9007199254740994',
                }
              : {},
          ),
        ),
      )
    })
    vi.stubGlobal('fetch', fetcher)
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    const mutation = store.resolveManually(TASK_ID, 'confirmed_not_executed')
    await flushPromises()
    expect(fetcher).toHaveBeenCalledTimes(3)
    // 此读取在 POST 完成前开始，允许返回确认前的版本；调用者不能把它当成确认后的事实。
    const background = store.loadSnapshot(TASK_ID)
    finishMutation(
      new Response(
        JSON.stringify({ task_id: TASK_ID, task_version: '9007199254740994' }),
      ),
    )
    await flushPromises()
    finishBackground(new Response(JSON.stringify(actionSnapshot())))
    await Promise.all([mutation, background])
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740994')
    expect(store.snapshots[TASK_ID]?.status).toBe('failed')
    expect(reads).toBe(4)
    expect(store.snapshotErrors[TASK_ID]).toBeNull()
  })

  it('keeps a new owner snapshot and errors when an old manual conflict recovery finishes late', async () => {
    let reads = 0
    let submissions = 0
    let finishRecovery: (response: Response) => void = () => undefined
    const freshSnapshot = actionSnapshot({
      status: 'failed',
      event_cursor: '9007199254740995',
      task_version: '9007199254740995',
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.endsWith('/manual-resolution')) {
          submissions += 1
          return new Response(
            JSON.stringify({
              title: 'Conflict',
              error_code: 'task_version_conflict',
              trace_id: 'synthetic-old-owner-conflict',
            }),
            { status: 409 },
          )
        }
        reads += 1
        if (reads === 3)
          return new Promise<Response>((resolve) => {
            finishRecovery = resolve
          })
        return new Response(
          JSON.stringify(reads === 4 ? freshSnapshot : actionSnapshot()),
        )
      }),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    const mutation = store.resolveManually(TASK_ID, 'confirmed_not_executed')
    // 提前监听原始失败，避免把预期 409 当成未处理 rejection；清理不能吞掉调用方异常。
    const rejected = expect(mutation).rejects.toMatchObject({
      name: 'ProblemError',
      problem: {
        status: 409,
        error_code: 'task_version_conflict',
        trace_id: 'synthetic-old-owner-conflict',
      },
    })
    await flushPromises()
    expect(reads).toBe(3)
    expect(submissions).toBe(1)
    store.clear()
    await store.loadSnapshot(TASK_ID)
    const acceptedSnapshot = store.snapshots[TASK_ID]
    expect(acceptedSnapshot?.task_version).toBe('9007199254740995')
    expect(store.snapshotErrors[TASK_ID]).toBeNull()
    expect(store.snapshotErrors[TASK_ID]?.trace_id).toBeUndefined()

    // 旧 catch 的恢复 GET 跨越 clear；它和外层错误处理都必须服从新 owner 的边界。
    finishRecovery(new Response(JSON.stringify(actionSnapshot())))
    await rejected
    expect(store.snapshots[TASK_ID]).toBe(acceptedSnapshot)
    expect(store.snapshots[TASK_ID]?.status).toBe('failed')
    expect(store.snapshotErrors[TASK_ID]).toBeNull()
    expect(store.snapshotErrors[TASK_ID]?.trace_id).toBeUndefined()
    expect(store.snapshotLoading[TASK_ID]).toBe(false)
    expect(store.mutating[TASK_ID]).toBeUndefined()
    expect(reads).toBe(4)
    expect(submissions).toBe(1)
  })

  it('preserves a same-owner manual conflict and its safe trace after refreshing the snapshot', async () => {
    let reads = 0
    let submissions = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.endsWith('/manual-resolution')) {
          submissions += 1
          return new Response(
            JSON.stringify({
              title: 'Conflict',
              error_code: 'task_version_conflict',
              trace_id: 'synthetic-current-owner-conflict',
            }),
            { status: 409 },
          )
        }
        reads += 1
        return new Response(
          JSON.stringify(
            actionSnapshot(
              reads === 3
                ? {
                    event_cursor: '9007199254740994',
                    task_version: '9007199254740994',
                  }
                : {},
            ),
          ),
        )
      }),
    )
    const store = useActionsStore()
    await store.loadSnapshot(TASK_ID)
    await expect(
      store.resolveManually(TASK_ID, 'confirmed_not_executed'),
    ).rejects.toMatchObject({
      name: 'ProblemError',
      problem: { status: 409, trace_id: 'synthetic-current-owner-conflict' },
    })
    expect(store.snapshots[TASK_ID]?.task_version).toBe('9007199254740994')
    expect(store.snapshotErrors[TASK_ID]).toEqual({
      message: '无法刷新操作，请重试。',
      trace_id: 'synthetic-current-owner-conflict',
    })
    expect(store.mutating[TASK_ID]).toBe(false)
    expect(reads).toBe(3)
    expect(submissions).toBe(1)
  })

  it('preserves the last good list when refresh fails and exposes a safe trace', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: actionItems(), limit: 50, offset: 0 }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            title: 'Unavailable',
            trace_id: 'synthetic-trace',
            error_code: 'unavailable',
          }),
          { status: 503 },
        ),
      )
    vi.stubGlobal('fetch', fetcher)
    const store = useActionsStore()
    await store.refreshList()
    await store.refreshList()
    expect(store.items).toHaveLength(3)
    expect(store.listError?.trace_id).toBe('synthetic-trace')
    expect(store.loading).toBe(false)
  })

  it('rejects late responses after the owner clears sensitive memory', async () => {
    let complete: (response: Response) => void = () => undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            complete = resolve
          }),
      ),
    )
    const store = useActionsStore()
    const pending = store.loadSnapshot(TASK_ID)
    store.clear()
    complete(new Response(JSON.stringify(actionSnapshot())))
    await pending
    expect(Object.keys(store.snapshots)).toHaveLength(0)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })
})

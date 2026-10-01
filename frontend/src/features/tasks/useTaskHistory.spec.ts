import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref } from 'vue'
import { listTaskHistory, type TaskHistoryPage } from '@/api/taskHistory'
import { ProblemError } from '@/api/client'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { historyItem, historyPage } from '@/test-support/taskHistoryFixtures'
import { useTaskHistory } from './useTaskHistory'

vi.mock('@/api/taskHistory', async (original) => ({ ...await original<typeof import('@/api/taskHistory')>(), listTaskHistory: vi.fn() }))
const api = vi.mocked(listTaskHistory)
let state: ReturnType<typeof useTaskHistory>
const timezone = ref<string | null>('Asia/Shanghai')
const Probe = defineComponent({ setup() {
  state = useTaskHistory({ timezone })
  return () => h('div', [state.page.value?.items[0]?.created_at ?? '空', state.hasNewTasks.value ? '有新任务' : ''])
} })
/** 排空路由与 Vue 微任务，不依赖真实时间。 */
async function flush() { for (let index = 0; index < 100; index++) await Promise.resolve(); await nextTick() }
/** 受控网络响应，使乱序及卸载窗口可重复。 */
function deferred() { let complete: ((page: TaskHistoryPage) => void) | undefined; const promise = new Promise<TaskHistoryPage>((done) => { complete = done }); return { promise, resolve: (page: TaskHistoryPage) => { if (!complete) throw new Error('Deferred not initialized'); complete(page) } } }
const newer = () => historyPage({ items: [historyItem({ created_at: '2026-10-02T00:00:00.000000Z' })], background_failed_count: 9 })
async function mount(route = '/tasks') { const view = await renderWithPlugins(Probe, { route }); await flush(); return view }
function visibility(value: 'visible' | 'hidden') { Object.defineProperty(document, 'visibilityState', { configurable: true, value }); document.dispatchEvent(new Event('visibilitychange')) }

beforeEach(() => { vi.useFakeTimers(); api.mockReset(); api.mockResolvedValue(historyPage()); timezone.value = 'Asia/Shanghai'; visibility('visible') })
afterEach(() => { vi.useRealTimers() })

describe('独立历史列表生命周期', () => {
  it('探测仅提示新增，不混合可见快照', async () => {
    api.mockResolvedValueOnce(historyPage()).mockResolvedValue(newer())
    const view = await mount()
    const original = state.page.value
    await vi.advanceTimersByTimeAsync(30_000)
    expect(view.getByText(/有新任务/)).toBeVisible()
    expect(state.page.value).toBe(original)
  })
  it('首次空页可发现新增', async () => {
    api.mockResolvedValueOnce(historyPage({ items: [] })).mockResolvedValue(newer())
    await mount(); await vi.advanceTimersByTimeAsync(30_000)
    expect(state.hasNewTasks.value).toBe(true)
    expect(state.page.value?.items).toEqual([])
  })
  it('深链接先建立最新页基线，后续新增才提示', async () => {
    api.mockResolvedValueOnce(historyPage()).mockResolvedValue(newer())
    await mount('/tasks?cursor=deep')
    expect(api).toHaveBeenCalledTimes(2)
    expect(state.cursor.value).toBe('deep')
    expect(state.hasNewTasks.value).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(state.hasNewTasks.value).toBe(false)
    api.mockResolvedValue(historyPage({ items: [historyItem({ created_at: '2026-10-03T00:00:00.000000Z' })] }))
    await vi.advanceTimersByTimeAsync(30_000)
    expect(state.hasNewTasks.value).toBe(true)
  })
  it('慢探测不并发，失败等待下个30秒', async () => {
    await mount(); const pending = deferred(); api.mockReturnValueOnce(pending.promise)
    await vi.advanceTimersByTimeAsync(90_000)
    expect(api).toHaveBeenCalledTimes(2)
    pending.resolve(historyPage()); await flush()
    api.mockRejectedValueOnce(new Error('synthetic'))
    await vi.advanceTimersByTimeAsync(30_000)
    expect(api).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(29_999)
    expect(api).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(api).toHaveBeenCalledTimes(4)
  })
  it('隐藏与卸载停止，恢复读取当前页且旧探测失效', async () => {
    const view = await mount('/tasks?cursor=deep'); const pending = deferred(); api.mockReturnValueOnce(pending.promise)
    await vi.advanceTimersByTimeAsync(30_000)
    visibility('hidden'); pending.resolve(newer()); await flush()
    expect(state.hasNewTasks.value).toBe(false)
    const count = api.mock.calls.length
    await vi.advanceTimersByTimeAsync(90_000); expect(api).toHaveBeenCalledTimes(count)
    visibility('visible'); await flush()
    expect(api.mock.calls[count]?.[1]).toBe('deep')
    view.unmount(); const stopped = api.mock.calls.length
    await vi.advanceTimersByTimeAsync(90_000); expect(api).toHaveBeenCalledTimes(stopped)
  })
  it('旧页响应不覆盖新筛选，task_id变化不重载', async () => {
    const pending = deferred(); api.mockReturnValueOnce(pending.promise)
    const view = await mount()
    await state.setFilters({ ...state.filters.value, scope: 'all' })
    expect(state.filters.value.scope).toBe('all')
    pending.resolve(newer()); await flush()
    expect(state.page.value?.items[0]?.created_at).toBe(historyItem().created_at)
    const count = api.mock.calls.length
    await view.router.push({ query: { ...view.router.currentRoute.value.query, task_id: 'synthetic' } }); await flush()
    expect(api).toHaveBeenCalledTimes(count)
  })
  it('旧探测不能把新筛选标记为新增', async () => {
    await mount(); const pending = deferred(); api.mockReturnValueOnce(pending.promise)
    await vi.advanceTimersByTimeAsync(30_000)
    await state.setFilters({ ...state.filters.value, scope: 'all' })
    pending.resolve(newer()); await flush()
    expect(state.hasNewTasks.value).toBe(false)
  })
  it('慢探测跨筛选及隐藏恢复仍互斥，旧结果不污染且完成后再等30秒', async () => {
    await mount()
    const pending = deferred()
    api.mockReturnValueOnce(pending.promise)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(api).toHaveBeenCalledTimes(2)

    // 显式页面读取允许进行；未结束的旧探测仍占用唯一探测槽位。
    const filtered = historyPage({ items: [historyItem({ status: 'failed' })] })
    api.mockResolvedValue(filtered)
    await state.setFilters({ ...state.filters.value, scope: 'all', status: 'failed' })
    expect(api).toHaveBeenCalledTimes(3)
    const current = state.page.value
    visibility('hidden')
    await vi.advanceTimersByTimeAsync(90_000)
    expect(api).toHaveBeenCalledTimes(3)
    visibility('visible')
    await flush()
    expect(api).toHaveBeenCalledTimes(4)
    expect(api.mock.calls[3]).toEqual([{ ...state.filters.value, scope: 'all', status: 'failed' }, null])
    await vi.advanceTimersByTimeAsync(90_000)
    expect(api).toHaveBeenCalledTimes(4)

    // 旧筛选返回更晚时间戳也不能改快照、计数或新任务提示。
    pending.resolve(newer())
    await flush()
    expect(state.page.value).toBe(current)
    expect(state.page.value?.background_failed_count).toBe(filtered.background_failed_count)
    expect(state.hasNewTasks.value).toBe(false)
    expect(state.error.value).toBeNull()
    expect(state.loading.value).toBe(false)
    await vi.advanceTimersByTimeAsync(29_999)
    expect(api).toHaveBeenCalledTimes(4)
    api.mockResolvedValue(historyPage({ items: [historyItem({ status: 'failed', created_at: '2026-10-03T00:00:00.000000Z' })] }))
    await vi.advanceTimersByTimeAsync(1)
    expect(api).toHaveBeenCalledTimes(5)
    expect(api.mock.calls[4]).toEqual([{ ...state.filters.value, scope: 'all', status: 'failed' }, null])
    expect(state.hasNewTasks.value).toBe(true)
    expect(state.page.value).toBe(current)
  })
  it('非法URL零请求，错误固定且不伪造ProblemError', async () => {
    await mount('/tasks?status=bad')
    expect(api).not.toHaveBeenCalled()
    expect(state.error.value).not.toBeInstanceOf(ProblemError)
    expect(state.error.value).toMatchObject({ kind: 'local' })
  })
  it('游标失效保留旧页和真实错误，只允许显式回首页', async () => {
    const view = await mount()
    const previous = state.page.value
    const problem = new ProblemError({ type: 'about:blank', title: 'Invalid cursor', status: 422, error_code: 'task_history_cursor_invalid', trace_id: 'synthetic', detail: '', instance: '/tasks' })
    api.mockRejectedValueOnce(problem)
    await view.router.push('/tasks?cursor=expired'); await flush()
    expect(state.error.value).toBe(problem)
    expect(state.page.value).toBe(previous)
    expect(state.cursor.value).toBe('expired')
    await state.refreshFirst()
    expect(state.cursor.value).toBeNull()
    expect(state.error.value).toBeNull()
  })
  it('timezone初次就绪保留深链接，随后变更重置', async () => {
    timezone.value = null
    await mount('/tasks?cursor=deep'); expect(api).not.toHaveBeenCalled()
    timezone.value = 'Asia/Shanghai'; await flush()
    expect(api.mock.calls[0]?.[1]).toBe('deep')
    timezone.value = 'UTC'; await flush()
    expect(state.cursor.value).toBeNull()
    expect(api.mock.calls.at(-1)?.[1]).toBeNull()
  })
  it('翻页与手动刷新等待读取完成，不写浏览器存储', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    api.mockResolvedValue(historyPage({ next_cursor: 'older', previous_cursor: 'newer' }))
    await mount()
    await state.nextPage(); expect(state.cursor.value).toBe('older')
    await state.previousPage(); expect(state.cursor.value).toBe('newer')
    await state.refreshCurrent(); expect(api.mock.calls.at(-1)?.[1]).toBe('newer')
    await state.refreshFirst(); expect(state.cursor.value).toBeNull()
    expect(storage).not.toHaveBeenCalled(); storage.mockRestore()
  })
  it('刷新Promise等待真实响应，卸载后旧响应不写入', async () => {
    const view = await mount()
    const original = state.page.value
    const pending = deferred(); api.mockReturnValueOnce(pending.promise)
    let finished = false
    const refresh = state.refreshCurrent().then(() => { finished = true })
    await flush()
    expect(finished).toBe(false)
    expect(state.loading.value).toBe(true)
    view.unmount(); pending.resolve(newer()); await refresh
    expect(state.page.value).toBe(original)
  })
  it('本地无效表单不改URL或发请求，未知异常不泄漏原始消息', async () => {
    const view = await mount()
    const originalRoute = view.router.currentRoute.value.fullPath
    await state.setFilters({ ...state.filters.value, created_from_date: '2026-02-30' })
    expect(api).toHaveBeenCalledTimes(1)
    expect(view.router.currentRoute.value.fullPath).toBe(originalRoute)
    expect(state.error.value).toMatchObject({ kind: 'local' })
    api.mockRejectedValueOnce(new Error('synthetic-private-payload'))
    await state.refreshCurrent()
    expect(state.error.value).toEqual({ kind: 'local', message: '任务历史加载失败，请重试。' })
    expect(state.page.value?.items[0]?.created_at).toBe(historyItem().created_at)
  })
  it('导航拒绝不伪造成功', async () => {
    const view = await mount(); view.router.beforeEach(() => false)
    await expect(state.setFilters({ ...state.filters.value, scope: 'all' })).rejects.toThrow()
    expect(state.filters.value.scope).toBe('business')
  })
})

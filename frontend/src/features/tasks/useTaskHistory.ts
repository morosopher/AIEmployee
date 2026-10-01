import { computed, onMounted, onUnmounted, ref, shallowRef, watch, type Ref } from 'vue'
import { isNavigationFailure, NavigationFailureType, useRoute, useRouter, type LocationQuery } from 'vue-router'
import { ProblemError } from '@/api/client'
import { historyItemKey, listTaskHistory, type TaskHistoryFilters, type TaskHistoryPage } from '@/api/taskHistory'
import { historyRouteQuery, parseHistoryRoute, type HistoryLocalError } from './historyRoute'

/** 保留真实服务端错误身份；网络及本地异常只显示固定说明，禁止透传敏感载荷。 */
function toHistoryError(cause: unknown): ProblemError | HistoryLocalError {
  return cause instanceof ProblemError ? cause : { kind: 'local', message: '任务历史加载失败，请重试。' }
}

/**
 * 在页面 setup 中管理只读摘要、URL 与可见性探测，不接触完整 Task Store/SSE。
 * @param options.timezone 当前用户真实时区；null 时等待，首次就绪不改变深链接。
 * @returns 独立响应式列表与异步导航方法；导航失败抛出，读取失败保留旧页并展示错误。
 */
export function useTaskHistory({ timezone }: { timezone: Readonly<Ref<string | null>> }) {
  const route = useRoute()
  const router = useRouter()
  const parsed = computed(() => parseHistoryRoute(route.query))
  const filters = computed(() => parsed.value.filters)
  const cursor = computed(() => parsed.value.cursor)
  const page = shallowRef<TaskHistoryPage | null>(null)
  const error = shallowRef<ProblemError | HistoryLocalError | null>(null)
  const loading = ref(false)
  const hasNewTasks = ref(false)
  let generation = 0
  let mounted = false
  let disposed = false
  let probing = false
  let baseline: string | null | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let currentRead: Promise<void> = Promise.resolve()
  let knownTimezone: string | null = null
  const visible = () => document.visibilityState === 'visible'
  const active = () => mounted && !disposed && visible() && timezone.value !== null && !parsed.value.error

  /** 停止后续调度；在途请求靠世代失效，避免把慢请求误当成可并发空闲。 */
  function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  function schedule() {
    clearTimer()
    if (active()) timer = setTimeout(() => { void probe() }, 30_000)
  }

  /**
   * 同一时刻最多一个探测，完成后再等30秒。探测只能更新基线/提示，不更新可见快照。
   * 深链接的首次探测建立当前筛选头部，避免把既存任务误报为新增。
   */
  async function probe(): Promise<void> {
    if (!active() || probing || loading.value) { schedule(); return }
    probing = true
    const requestGeneration = generation
    try {
      const result = await listTaskHistory(filters.value, null)
      if (!active() || requestGeneration !== generation) return
      const head = result.items[0] ? historyItemKey(result.items[0]) : null
      if (baseline === undefined) baseline = head
      else if (head !== null && (baseline === null || head > baseline)) hasNewTasks.value = true
    } catch {
      // 探测失败不隐藏当前列表，也不快速重试；显式读取失败由 error 承载。
    } finally {
      probing = false
      schedule()
    }
  }

  /** 每次显式读取使旧页/探测失效；失败保留原快照，不自动丢弃无效游标。 */
  async function readCurrentPage(): Promise<void> {
    const requestGeneration = ++generation
    clearTimer()
    error.value = parsed.value.error
    if (!active()) { loading.value = false; return }
    loading.value = true
    const requestCursor = cursor.value
    try {
      const result = await listTaskHistory(filters.value, requestCursor)
      if (!active() || requestGeneration !== generation) return
      page.value = result
      error.value = null
      if (requestCursor === null) {
        baseline = result.items[0] ? historyItemKey(result.items[0]) : null
        hasNewTasks.value = false
      }
    } catch (cause) {
      if (!disposed && requestGeneration === generation) error.value = toHistoryError(cause)
    } finally {
      if (!disposed && requestGeneration === generation) {
        loading.value = false
        if (!error.value && baseline === undefined && requestCursor !== null) void probe()
        else schedule()
      }
    }
  }

  /** 统一记录当前请求，导航 Promise 直到对应读取落定才结束。 */
  function reload(): Promise<void> { currentRead = readCurrentPage(); return currentRead }

  // 只依赖历史字段；task_id 选择不会新增列表请求或第二条详情订阅。
  const routeKey = computed(() => JSON.stringify(parsed.value))
  const filterKey = computed(() => JSON.stringify(filters.value))
  watch(routeKey, () => { void reload() }, { flush: 'sync' })
  watch(filterKey, () => { baseline = undefined; hasNewTasks.value = false }, { flush: 'sync' })

  /**
   * 先验证后导航；中止/取消必须向调用方报告，不伪造 URL 已更新。
   * @param next 完整公开筛选。
   * @param nextCursor 目标分页游标。
   */
  async function navigate(next: TaskHistoryFilters, nextCursor: string | null): Promise<void> {
    const query = historyRouteQuery(route.query, next, nextCursor)
    const validation = parseHistoryRoute(query as LocationQuery)
    if (validation.error) { error.value = validation.error; return }
    const before = routeKey.value
    const failure = await router.push({ query })
    if (failure && !isNavigationFailure(failure, NavigationFailureType.duplicated)) throw failure
    if (before === routeKey.value) await reload()
    else await currentRead
  }

  /** 改变筛选从首页开始；无效格式保留现有 URL。 */
  async function setFilters(next: TaskHistoryFilters): Promise<void> { await navigate(next, null) }
  /** 使用服务端相邻页游标；无下一页时不发请求。 */
  async function nextPage(): Promise<void> { if (page.value?.next_cursor) await navigate(filters.value, page.value.next_cursor) }
  /** 使用服务端反向游标；无上一页时不发请求。 */
  async function previousPage(): Promise<void> { if (page.value?.previous_cursor) await navigate(filters.value, page.value.previous_cursor) }
  /** 动作完成或恢复可见时刷新当前页，保留分页位置。 */
  async function refreshCurrent(): Promise<void> { await reload() }
  /** 用户主动刷新或显式恢复无效游标时重新建立最新第一页。 */
  async function refreshFirst(): Promise<void> { await navigate(filters.value, null) }

  watch(timezone, (value) => {
    if (value === null) { ++generation; clearTimer(); loading.value = false; return }
    const changed = knownTimezone !== null && knownTimezone !== value
    knownTimezone = value
    if (!mounted) return
    if (changed) {
      ++generation; baseline = undefined; hasNewTasks.value = false
      void refreshFirst().catch((cause: unknown) => { if (!disposed) error.value = toHistoryError(cause) })
    } else void reload()
  }, { immediate: true, flush: 'sync' })

  function visibilityChanged() {
    ++generation; clearTimer(); loading.value = false
    if (visible()) void reload()
  }
  onMounted(() => { mounted = true; document.addEventListener('visibilitychange', visibilityChanged); void reload() })
  onUnmounted(() => { disposed = true; mounted = false; ++generation; clearTimer(); document.removeEventListener('visibilitychange', visibilityChanged) })

  return { page, filters, cursor, loading, error, hasNewTasks, setFilters, nextPage, previousPage, refreshCurrent, refreshFirst }
}

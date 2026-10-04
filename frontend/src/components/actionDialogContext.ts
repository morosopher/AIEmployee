import type { InjectionKey } from 'vue'

/**
 * 人工结果 Dialog 到 AppShell 的展示端口，仅传递焦点元素和隔离生命周期。
 * @param trigger 打开前的触发元素；可为空或在关闭前被移除。
 * @returns 当前实例的幂等释放函数：关闭动画结束或卸载后调用，等待背景更新再归还焦点。
 * 不包含业务快照、确认结果或请求，不能替代调用方的 confirm/cancel 契约。
 */
export type OpenActionDialog = (
  trigger: HTMLElement | null,
) => () => Promise<void>

/** 深层组件通过注入直达外壳，不依赖 Vue 自定义事件跨组件冒泡。 */
export const actionDialogContext: InjectionKey<OpenActionDialog> = Symbol(
  'action-dialog-context',
)

/** 浏览器传输替身只分发已监听的 named event；未知事件只能由后续 heartbeat 暴露游标。 */
export class TaskEventSource extends EventTarget {
  static instances: TaskEventSource[] = []
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  closed = false

  /** @param url 仅记录任务 ID 与持久游标，不含内容。 */
  constructor(readonly url: string) {
    super()
    TaskEventSource.instances.push(this)
  }

  /** 关闭流后仍可人为交付已排队事件，用于验证产品的旧连接身份检查。 */
  close(): void {
    this.closed = true
  }

  /** @param name 实际 named event。 @param data 合成安全载荷。 @param lastEventId 平台保留的游标。 */
  emit(name: string, data: unknown, lastEventId = ''): void {
    this.dispatchEvent(
      new MessageEvent(name, { data: JSON.stringify(data), lastEventId }),
    )
  }
}

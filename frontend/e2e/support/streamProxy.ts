import { createServer, request as forward, type ClientRequest, type ServerResponse } from 'node:http'

/** 单个真实浏览器连接的传输旁证；不保存 Cookie、请求正文或 SSE data。 */
export interface StreamProxy {
  origin: string
  cursors: Array<string | null>
  disconnect: () => void
  resume: () => void
  close: () => Promise<void>
}

/**
 * 在 loopback 临时端口逐字节转发真实 Vite/API 流，只允许切断本测试拥有的 SSE socket。
 * @param eventPath 本例精确 task 的事件路径；其他 API、静态资源及正文全部原样转发。
 * @returns 真实请求游标和有界资源生命周期。断线时销毁连接，不制造任何业务响应或事件。
 */
export async function streamProxy(eventPath: string): Promise<StreamProxy> {
  const streams = new Set<ServerResponse>()
  const upstreams = new Set<ClientRequest>()
  const cursors: Array<string | null> = []
  let disconnected = false
  const server = createServer((incoming, outgoing) => {
    const url = new URL(incoming.url ?? '/', 'http://127.0.0.1:5173')
    if (url.pathname === eventPath) {
      const header = incoming.headers['last-event-id']
      cursors.push(typeof header === 'string' ? header : url.searchParams.get('last_event_id'))
      if (disconnected) {
        outgoing.destroy()
        return
      }
      streams.add(outgoing)
    }
    const upstream = forward(url, { method: incoming.method, headers: incoming.headers }, (response) => {
      outgoing.writeHead(response.statusCode ?? 502, response.headers)
      response.pipe(outgoing)
      response.on('error', () => outgoing.destroy())
    })
    upstreams.add(upstream)
    upstream.on('error', () => outgoing.destroy())
    outgoing.on('close', () => {
      streams.delete(outgoing)
      upstreams.delete(upstream)
      upstream.destroy()
    })
    incoming.pipe(upstream)
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Synthetic stream proxy has no port')
  return {
    origin: `http://127.0.0.1:${address.port}`,
    cursors,
    disconnect: () => {
      disconnected = true
      for (const stream of streams) stream.destroy()
    },
    resume: () => { disconnected = false },
    close: async () => {
      for (const upstream of upstreams) upstream.destroy()
      server.closeAllConnections()
      await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
    },
  }
}

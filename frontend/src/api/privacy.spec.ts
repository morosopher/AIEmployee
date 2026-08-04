import { describe, expect, it, vi } from 'vitest'

import { requestSourceCacheDeletion, requestAllDataDeletion } from './privacy'

describe('privacy api', () => {
  it('posts source-cache deletion and preserves the accepted task id', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ task_id: 'task-source-1', status: 'queued' }),
    }))
    await expect(requestSourceCacheDeletion('idem-source')).resolves.toEqual({
      task_id: 'task-source-1',
      status: 'queued',
    })
    const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('idem-source')
  })

  it('requires the exact all-data confirmation phrase', async () => {
    expect(() => requestAllDataDeletion('wrong', 'idem-all')).toThrow('DELETE ALL DATA')
  })
})

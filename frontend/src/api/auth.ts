import { requestJson } from './client'
import { parseSession } from './types'
export const listSessions = () => requestJson('/auth/sessions', (v) => { if (!Array.isArray(v)) throw new Error('Invalid sessions response'); return v.map(parseSession) })
export const revokeSession = (id: string) => requestJson(`/auth/sessions/${encodeURIComponent(id)}`, () => null, { method: 'DELETE' })

import { requestJson } from './client'
import type { Session } from './types'
export const listSessions = () => requestJson('/auth/sessions', (v) => v as Session[])
export const revokeSession = (id: string) => requestJson(`/auth/sessions/${encodeURIComponent(id)}`, () => null, { method: 'DELETE' })

import { requestJson } from './client'
import type { Connection } from './types'
export const listConnections = () => requestJson('/connections', (v) => v as Connection[])
export const startGoogleConnection = () => requestJson('/connections/google/start', (v) => v as { authorization_url: string }, { method: 'POST' })
export const syncConnection = (id: string) => requestJson(`/connections/${encodeURIComponent(id)}/sync`, (v) => v as { gmail_task_id: string; calendar_task_id: string }, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } })
export const disconnectConnection = (id: string) => requestJson(`/connections/${encodeURIComponent(id)}`, () => null, { method: 'DELETE' })

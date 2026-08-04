import { requestJson } from './client'
import type { Brief } from './types'
const parseBrief = (value: unknown): Brief => value as Brief
export const getTodayBrief = () => requestJson('/briefs/today', parseBrief)
export const listBriefs = (localDate: string) => requestJson(`/briefs?local_date=${encodeURIComponent(localDate)}`, (v) => (Array.isArray(v) ? v.map(parseBrief) : []))
export const getBrief = (id: string) => requestJson(`/briefs/${encodeURIComponent(id)}`, parseBrief)
export const generateBrief = () => requestJson('/briefs/generate', (v) => v as { task_id: string }, { method: 'POST' })

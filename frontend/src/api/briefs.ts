import { requestJson } from './client'
import { parseBrief } from './types'
export const getTodayBrief = () => requestJson('/briefs/today', parseBrief)
export const listBriefs = (localDate: string) => requestJson(`/briefs?local_date=${encodeURIComponent(localDate)}`, (v) => { if (!Array.isArray(v)) throw new Error('Invalid briefs response'); return v.map(parseBrief) })
export const getBrief = (id: string) => requestJson(`/briefs/${encodeURIComponent(id)}`, parseBrief)
export const generateBrief = () => requestJson('/briefs/generate', (v) => v as { task_id: string }, { method: 'POST' })

import { requestJson } from './client'
import type { UserSettings } from './types'
export const getSettings = () => requestJson('/settings', (v) => v as UserSettings)
export const updateSettings = (settings: Partial<UserSettings>) => requestJson('/settings', (v) => v as UserSettings, { method: 'PATCH', body: JSON.stringify(settings) })

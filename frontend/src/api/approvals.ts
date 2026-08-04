import { requestJson } from './client'
export const decideApproval = (id: string, decision: 'approved' | 'rejected', version: number, payload_hash: string) => requestJson(`/approvals/${encodeURIComponent(id)}/decision`, () => null, { method: 'POST', body: JSON.stringify({ decision, version, payload_hash }) })

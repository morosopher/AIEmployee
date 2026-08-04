import { requestJson } from './client'
export async function decideApproval(id: string, decision: 'approved' | 'rejected', version: number, payload_hash: string): Promise<void> {
  await requestJson(`/approvals/${encodeURIComponent(id)}/decision`, () => null, { method: 'POST', body: JSON.stringify({ decision, version, payload_hash }) })
}

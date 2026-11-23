import { request } from './client'
import type { Acceptance, CheckpointRow } from '@/data/types'

async function asJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export type EligibleResult = {
  task: string
  total: number
  items: CheckpointRow[]
  rule: string
}

export type AcceptanceResult = {
  ok: boolean
  message: string
  acceptance: Acceptance
}

export async function fetchEligibleCheckpoints(task: string): Promise<EligibleResult> {
  return asJson<EligibleResult>(
    `/api/record_checkpoint/eligible?task=${encodeURIComponent(task)}`,
  )
}

export async function fetchCheckpointReferences(checkpointId: number) {
  return asJson<{
    checkpoint: CheckpointRow
    reference_count: number
    acceptances: Acceptance[]
    handovers: unknown[]
  }>(`/api/record_checkpoint/${checkpointId}/references`)
}

export async function fetchTaskAcceptances(task: string) {
  return asJson<{
    task: string
    total: number
    items: Acceptance[]
    latest_eligible_checkpoint: CheckpointRow | null
  }>(`/api/resume_policy/tasks/acceptances?task=${encodeURIComponent(task)}`)
}

export async function acceptResume(payload: {
  policy_id: number
  task: string
  partition?: string
  last_offset?: number | null
}): Promise<AcceptanceResult> {
  return asJson<AcceptanceResult>('/api/resume_policy/accept', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function readNextSegment(acceptanceId: number) {
  return asJson<{ ok: boolean; message: string; segment: unknown; acceptance: Acceptance }>(
    `/api/resume_policy/acceptances/${acceptanceId}/read-segment`,
    { method: 'POST', body: JSON.stringify({}) },
  )
}

export async function handoverCheckpoint(acceptanceId: number, newCheckpointId: number) {
  return asJson<{ ok: boolean; message: string; handover: unknown; successor: Acceptance }>(
    `/api/resume_policy/acceptances/${acceptanceId}/handover`,
    { method: 'POST', body: JSON.stringify({ new_checkpoint_id: newCheckpointId }) },
  )
}

export async function publishRound(acceptanceId: number): Promise<AcceptanceResult> {
  return asJson<AcceptanceResult>(
    `/api/resume_policy/acceptances/${acceptanceId}/publish`,
    { method: 'POST', body: JSON.stringify({}) },
  )
}

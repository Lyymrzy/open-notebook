import apiClient from './client'

/**
 * Polling helper for surreal-commands background jobs.
 *
 * Async work (an LLM pass over a notebook) runs on the worker and reports
 * through `GET /commands/jobs/{id}`. Statuses are queued | running | completed
 * | failed | canceled (+ unknown when the record is gone, e.g. after an API
 * restart with the in-memory job store).
 */

export interface CommandJobStatusResponse {
  job_id: string
  status: string
  result?: Record<string, unknown> | null
  error_message?: string | null
  created?: string | null
  updated?: string | null
  progress?: Record<string, unknown> | null
}

export interface JobOutcome {
  ok: boolean
  status: string
  result?: Record<string, unknown> | null
  errorMessage?: string | null
}

const TERMINAL_FAILURES = new Set(['failed', 'canceled'])
// A single 'unknown' can happen on a stale read right after submission; a run of
// them means the job record is really gone (in-memory store + API restart).
const UNKNOWN_TOLERANCE = 3

export const jobsApi = {
  getStatus: async (jobId: string) => {
    const response = await apiClient.get<CommandJobStatusResponse>(
      `/commands/jobs/${jobId}`
    )
    return response.data
  },
}

export async function waitForJob(
  jobId: string,
  options?: { intervalMs?: number; timeoutMs?: number }
): Promise<JobOutcome> {
  const intervalMs = options?.intervalMs ?? 1500
  const timeoutMs = options?.timeoutMs ?? 10 * 60 * 1000
  const deadline = Date.now() + timeoutMs
  let unknownStreak = 0

  while (Date.now() < deadline) {
    try {
      const status = await jobsApi.getStatus(jobId)

      if (status.status === 'completed') {
        return {
          ok: true,
          status: status.status,
          result: status.result ?? null,
          errorMessage: null,
        }
      }
      if (TERMINAL_FAILURES.has(status.status)) {
        return {
          ok: false,
          status: status.status,
          result: status.result ?? null,
          errorMessage: status.error_message ?? null,
        }
      }
      if (status.status === 'unknown') {
        unknownStreak += 1
        if (unknownStreak >= UNKNOWN_TOLERANCE) {
          return {
            ok: false,
            status: status.status,
            errorMessage: status.error_message ?? null,
          }
        }
      } else {
        unknownStreak = 0
      }
    } catch {
      // Transient read failure: keep polling until the deadline rather than
      // reporting a failure the worker never produced.
      unknownStreak = 0
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }

  return { ok: false, status: 'timeout', errorMessage: null }
}

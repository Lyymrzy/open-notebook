import apiClient from './client'
import {
  ExplorationPoint,
  IterateJobResponse,
  KnowledgeGraphResponse,
  RefineNoteRequest,
  SynthesizeNotesRequest,
} from '@/lib/types/graph'

export const graphApi = {
  get: async (params?: {
    notebook_id?: string
    include_explorations?: boolean
    max_nodes?: number
  }) => {
    const response = await apiClient.get<KnowledgeGraphResponse>('/graph', { params })
    return response.data
  },
}

export const explorationsApi = {
  list: async (params?: { notebook_id?: string; status?: string }) => {
    const response = await apiClient.get<ExplorationPoint[]>('/explorations', {
      params,
    })
    return response.data
  },

  accept: async (id: string) => {
    const response = await apiClient.post<ExplorationPoint>(
      `/explorations/${id}/accept`
    )
    return response.data
  },

  dismiss: async (id: string) => {
    const response = await apiClient.post<ExplorationPoint>(
      `/explorations/${id}/dismiss`
    )
    return response.data
  },

  /** Retire a proposal that has produced knowledge (status: consumed). */
  consume: async (id: string) => {
    const response = await apiClient.post<ExplorationPoint>(
      `/explorations/${id}/consume`
    )
    return response.data
  },

  /** Submit a frontier scan; it runs on the worker, poll the returned job id. */
  scan: async (data: {
    notebook_id: string
    max_leaves?: number
    questions_per_leaf?: number
    model_id?: string
  }) => {
    const response = await apiClient.post<{
      job_id: string
      status: string
      notebook_id: string
    }>('/explorations/scan', data)
    return response.data
  },
}

export const iterateApi = {
  /** Consolidate notes/sources into a new knowledge note (async job). */
  synthesize: async (data: SynthesizeNotesRequest) => {
    const response = await apiClient.post<IterateJobResponse>(
      '/notes/iterate/synthesize',
      data
    )
    return response.data
  },

  /** Propose a revision; nothing is written to the note itself (async job). */
  refine: async (data: RefineNoteRequest) => {
    const response = await apiClient.post<IterateJobResponse>(
      '/notes/iterate/refine',
      data
    )
    return response.data
  },
}

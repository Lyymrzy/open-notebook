export interface Transformation {
  id: string
  name: string
  title: string
  description: string
  prompt: string
  apply_default: boolean
  model_id: string | null
  created: string
  updated: string
}

export interface CreateTransformationRequest {
  name: string
  title: string
  description: string
  prompt: string
  apply_default?: boolean
  model_id?: string | null
}

export interface UpdateTransformationRequest {
  name?: string
  title?: string
  description?: string
  prompt?: string
  apply_default?: boolean
  model_id?: string | null
}

export interface ExecuteTransformationRequest {
  transformation_id: string
  input_text: string
  model_id?: string | null
}

export interface ExecuteTransformationResponse {
  output: string
  transformation_id: string
  model_id: string | null
}

export type TransformationJobStatus =
  | 'queued'
  | 'running'
  | 'done'
  | 'error'

export interface TransformationJobSubmitResponse {
  job_id: string
  transformation_id: string
  status: string
  model_id: string | null
}

export interface TransformationJobStatusResponse {
  job_id: string
  status: TransformationJobStatus
  output: string | null
  error: string | null
  transformation_id: string | null
  model_id: string | null
  created: string | null
  started: string | null
  finished: string | null
}

export interface DefaultPrompt {
  transformation_instructions: string
}

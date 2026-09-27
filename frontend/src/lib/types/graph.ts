/**
 * Knowledge graph API types - mirror of api/models.py (migration 25).
 *
 * The graph is a projection: nodes are notes plus exploration points, edges are
 * the typed relations between them. `is_leaf` is computed server-side from the
 * `includes` edges so the UI and the frontier scan agree on where the tree ends.
 */

export type GraphNodeKind = 'note' | 'exploration'

export type GraphEdgeKind = 'includes' | 'relates_to' | 'explores'

export type ExplorationKind = 'gap' | 'depth' | 'crosslink' | 'contradiction'

export type ExplorationStatus = 'proposed' | 'accepted' | 'dismissed' | 'consumed'

export interface GraphNode {
  id: string
  kind: GraphNodeKind
  label: string
  note_type?: 'human' | 'ai' | null
  note_kind?: string | null
  status?: string | null
  tags: string[]
  is_leaf: boolean
  child_count: number
  exploration_kind?: ExplorationKind | null
  score?: number | null
  rationale?: string | null
  anchor_id?: string | null
  created?: string | null
  updated?: string | null
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  kind: GraphEdgeKind
  label?: string | null
}

export interface KnowledgeGraphResponse {
  nodes: GraphNode[]
  edges: GraphEdge[]
  notebook_id?: string | null
  max_nodes: number
  truncated: boolean
  /** Links whose other end lives outside the requested scope. */
  external_link_count: number
  counts: Record<string, number>
}

export interface ExplorationPoint {
  id: string
  question: string
  rationale?: string | null
  kind?: ExplorationKind | null
  status?: ExplorationStatus | null
  score?: number | null
  notebook_id?: string | null
  anchor_id?: string | null
  created?: string | null
  updated?: string | null
}

export interface IterateJobResponse {
  job_id: string
  status: string
  parent_note_id?: string | null
}

export interface SynthesizeNotesRequest {
  note_ids?: string[]
  source_ids?: string[]
  notebook_id?: string
  parent_note_id?: string
  instructions?: string
  model_id?: string
}

export interface RefineNoteRequest {
  note_id: string
  source_ids?: string[]
  instructions?: string
  model_id?: string
}

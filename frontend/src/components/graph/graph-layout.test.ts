import { describe, expect, it } from 'vitest'

import { layoutKnowledgeGraph, nodeSize } from './graph-layout'
import type { GraphEdge, GraphNode } from '@/lib/types/graph'

function note(id: string, overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    id,
    kind: 'note',
    label: id,
    tags: [],
    is_leaf: true,
    child_count: 0,
    ...overrides,
  }
}

function exploration(id: string, anchorId: string): GraphNode {
  return {
    id,
    kind: 'exploration',
    label: `Explore ${id}`,
    tags: [],
    is_leaf: true,
    child_count: 0,
    anchor_id: anchorId,
  }
}

function edge(
  kind: GraphEdge['kind'],
  source: string,
  target: string
): GraphEdge {
  return { id: `${kind}:${source}->${target}`, source, target, kind }
}

describe('layoutKnowledgeGraph', () => {
  it('returns nothing for an empty graph', () => {
    expect(layoutKnowledgeGraph([], [])).toEqual({ positions: {}, width: 0, height: 0 })
  })

  it('positions every node', () => {
    const nodes = [note('note:a'), note('note:b'), note('note:c')]
    const { positions } = layoutKnowledgeGraph(nodes, [])

    expect(Object.keys(positions).sort()).toEqual(['note:a', 'note:b', 'note:c'])
  })

  it('places a subtopic below the note that includes it', () => {
    const nodes = [note('note:parent', { is_leaf: false, child_count: 1 }), note('note:child')]
    const { positions } = layoutKnowledgeGraph(nodes, [edge('includes', 'note:parent', 'note:child')])

    expect(positions['note:parent'].y).toBeLessThan(positions['note:child'].y)
  })

  it('hangs an exploration point below its anchor', () => {
    // Stored proposal -> note, drawn note -> proposal: the proposal is what
    // could grow *under* the note, so it belongs below it.
    const nodes = [note('note:leaf'), exploration('exploration_point:e1', 'note:leaf')]
    const { positions } = layoutKnowledgeGraph(
      nodes,
      [edge('explores', 'exploration_point:e1', 'note:leaf')]
    )

    expect(positions['note:leaf'].y).toBeLessThan(positions['exploration_point:e1'].y)
  })

  it('does not let a cross-reference change the tree ranks', () => {
    const nodes = [note('note:a'), note('note:b')]
    const withoutWeb = layoutKnowledgeGraph(nodes, []).positions
    const withWeb = layoutKnowledgeGraph(nodes, [edge('relates_to', 'note:a', 'note:b')])
      .positions

    expect(withWeb).toEqual(withoutWeb)
  })

  it('ignores edges pointing at nodes that are not in the graph', () => {
    const nodes = [note('note:a')]
    const { positions } = layoutKnowledgeGraph(nodes, [
      edge('includes', 'note:a', 'note:not-loaded'),
      edge('relates_to', 'note:not-loaded', 'note:a'),
    ])

    expect(positions['note:a']).toBeDefined()
  })

  it('lays out a cycle without losing nodes', () => {
    const nodes = [note('note:a'), note('note:b')]
    const { positions } = layoutKnowledgeGraph(nodes, [
      edge('includes', 'note:a', 'note:b'),
      edge('includes', 'note:b', 'note:a'),
    ])

    expect(Object.keys(positions)).toHaveLength(2)
  })
})

describe('nodeSize', () => {
  it('gives exploration points their own box', () => {
    expect(nodeSize(exploration('exploration_point:e1', 'note:a')).height).toBeGreaterThan(
      nodeSize(note('note:a')).height
    )
  })
})

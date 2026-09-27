import dagre from 'dagre'
import type { GraphEdge, GraphNode } from '@/lib/types/graph'

/**
 * Layout for the knowledge graph.
 *
 * The graph is drawn top-down with the tree (`includes`) as the backbone; the
 * web (`relates_to`) only contributes long-range links, and an exploration point
 * hangs *below* the note it was proposed for - it is what could grow there next,
 * so it belongs under its anchor, not above it.
 *
 * dagre reverses cycles internally, but a malformed graph should never blank the
 * page, so a failed layout falls back to a plain grid.
 */

export const NOTE_NODE_WIDTH = 208
export const NOTE_NODE_HEIGHT = 58
export const EXPLORATION_NODE_WIDTH = 208
export const EXPLORATION_NODE_HEIGHT = 66

const GRID_COLUMNS = 6
const GRID_GAP_X = NOTE_NODE_WIDTH + 40
const GRID_GAP_Y = NOTE_NODE_HEIGHT + 40

export interface NodePosition {
  x: number
  y: number
}

export interface PositionedGraph {
  positions: Record<string, NodePosition>
  width: number
  height: number
}

export function nodeSize(node: GraphNode): { width: number; height: number } {
  return node.kind === 'exploration'
    ? { width: EXPLORATION_NODE_WIDTH, height: EXPLORATION_NODE_HEIGHT }
    : { width: NOTE_NODE_WIDTH, height: NOTE_NODE_HEIGHT }
}

export function layoutKnowledgeGraph(
  nodes: GraphNode[],
  edges: GraphEdge[]
): PositionedGraph {
  if (nodes.length === 0) {
    return { positions: {}, width: 0, height: 0 }
  }

  try {
    const graph = new dagre.graphlib.Graph()
    graph.setDefaultEdgeLabel(() => ({}))
    graph.setGraph({ rankdir: 'TB', nodesep: 44, ranksep: 96, marginx: 24, marginy: 24 })

    nodes.forEach((node) => {
      const { width, height } = nodeSize(node)
      graph.setNode(node.id, { width, height })
    })

    edges.forEach((edge) => {
      const { source, target } = edge
      if (!graph.hasNode(source) || !graph.hasNode(target)) return
      if (edge.kind === 'includes') {
        // Stored parent -> child already.
        graph.setEdge(source, target)
      } else if (edge.kind === 'explores') {
        // Stored proposal -> note; flip so the anchor ranks above the proposal.
        graph.setEdge(target, source)
      }
      // relates_to is deliberately not a layout constraint: it is a cross-link,
      // and letting it drive ranks would tangle the tree.
    })

    dagre.layout(graph)

    const positions: Record<string, NodePosition> = {}
    let maxX = 0
    let maxY = 0
    nodes.forEach((node) => {
      const laid = graph.node(node.id)
      if (!laid) return
      const { width, height } = nodeSize(node)
      // dagre reports centers; React Flow positions are top-left corners.
      const x = laid.x - width / 2
      const y = laid.y - height / 2
      positions[node.id] = { x, y }
      maxX = Math.max(maxX, x + width)
      maxY = Math.max(maxY, y + height)
    })

    if (Object.keys(positions).length === nodes.length) {
      return { positions, width: maxX, height: maxY }
    }
  } catch {
    // Fall through to the grid: a render is better than a blank canvas.
  }

  return gridLayout(nodes)
}

function gridLayout(nodes: GraphNode[]): PositionedGraph {
  const positions: Record<string, NodePosition> = {}
  nodes.forEach((node, index) => {
    positions[node.id] = {
      x: (index % GRID_COLUMNS) * GRID_GAP_X,
      y: Math.floor(index / GRID_COLUMNS) * GRID_GAP_Y,
    }
  })
  const rows = Math.ceil(nodes.length / GRID_COLUMNS)
  return {
    positions,
    width: Math.min(nodes.length, GRID_COLUMNS) * GRID_GAP_X,
    height: rows * GRID_GAP_Y,
  }
}

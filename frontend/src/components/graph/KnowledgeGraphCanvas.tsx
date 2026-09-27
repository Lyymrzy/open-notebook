'use client'

import { useMemo } from 'react'
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import {
  KnowledgeNode,
  type KnowledgeNodeData,
} from '@/components/graph/KnowledgeNode'
import { layoutKnowledgeGraph, nodeSize } from '@/components/graph/graph-layout'
import type { GraphEdge, KnowledgeGraphResponse } from '@/lib/types/graph'

const nodeTypes = { knowledge: KnowledgeNode }

// Colour language shared with the legend: grey = tree, blue = cross-reference,
// violet = a proposal that is not knowledge yet.
//
// Theme tokens hold complete colour values (`--muted-foreground: #565a61`), so
// they must be referenced as `var(--x)` directly. Passing one through the hsl()
// wrapper yields an invalid declaration that the browser drops, which painted
// the tree edges with no stroke at all — see src/lib/theme-tokens.test.ts.
const EDGE_COLOR: Record<GraphEdge['kind'], string> = {
  includes: 'var(--muted-foreground)',
  relates_to: '#60a5fa',
  explores: '#a78bfa',
}

const EDGE_STYLE: Record<GraphEdge['kind'], React.CSSProperties> = {
  includes: { stroke: EDGE_COLOR.includes, strokeWidth: 2 },
  relates_to: {
    stroke: EDGE_COLOR.relates_to,
    strokeWidth: 1.5,
    strokeDasharray: '5 4',
  },
  explores: {
    stroke: EDGE_COLOR.explores,
    strokeWidth: 1.5,
    strokeDasharray: '2 4',
  },
}

const ARROWED: Record<GraphEdge['kind'], boolean> = {
  includes: true,
  relates_to: false,
  explores: true,
}

interface KnowledgeGraphCanvasProps {
  graph: KnowledgeGraphResponse
  selectedNodeId?: string | null
  onSelectNode?: (nodeId: string) => void
}

export function KnowledgeGraphCanvas({
  graph,
  onSelectNode,
}: KnowledgeGraphCanvasProps) {
  const { nodes, edges } = useMemo(() => {
    const { positions } = layoutKnowledgeGraph(graph.nodes, graph.edges)

    const flowNodes: Node<KnowledgeNodeData>[] = graph.nodes.map((node) => ({
      id: node.id,
      type: 'knowledge',
      position: positions[node.id] ?? { x: 0, y: 0 },
      data: { node },
      ...nodeSize(node),
    }))

    const flowEdges: Edge[] = graph.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: 'smoothstep',
      style: EDGE_STYLE[edge.kind],
      label: edge.label ?? undefined,
      markerEnd: ARROWED[edge.kind]
        ? {
            type: MarkerType.ArrowClosed,
            width: 12,
            height: 12,
            color: EDGE_COLOR[edge.kind],
          }
        : undefined,
    }))

    return { nodes: flowNodes, edges: flowEdges }
  }, [graph])

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodeClick={(_, node) => onSelectNode?.(node.id)}
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1.2 }}
      minZoom={0.1}
      nodesDraggable={false}
      nodesConnectable={false}
      // React Flow renders into a 100%-sized box, so a parent without a definite
      // height collapses it to nothing; being explicit keeps the canvas filled.
      style={{ width: '100%', height: '100%' }}
      proOptions={{ hideAttribution: false }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}

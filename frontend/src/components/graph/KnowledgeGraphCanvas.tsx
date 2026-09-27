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
const EDGE_STYLE: Record<GraphEdge['kind'], React.CSSProperties> = {
  includes: { stroke: 'hsl(var(--border))', strokeWidth: 1.5 },
  relates_to: { stroke: '#60a5fa', strokeWidth: 1.2, strokeDasharray: '5 4' },
  explores: { stroke: '#a78bfa', strokeWidth: 1.2, strokeDasharray: '2 4' },
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
        ? { type: MarkerType.ArrowClosed, width: 12, height: 12 }
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
      proOptions={{ hideAttribution: false }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}

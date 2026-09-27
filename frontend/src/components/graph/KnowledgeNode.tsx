'use client'

import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Sparkles, User } from 'lucide-react'

import { cn } from '@/lib/utils'
import { useTranslation } from '@/lib/hooks/use-translation'
import { NOTE_NODE_WIDTH, EXPLORATION_NODE_WIDTH } from '@/components/graph/graph-layout'
import type { GraphNode } from '@/lib/types/graph'

export interface KnowledgeNodeData extends Record<string, unknown> {
  node: GraphNode
}

const EXPLORATION_KIND_KEYS: Record<string, string> = {
  gap: 'graph.kindGap',
  depth: 'graph.kindDepth',
  crosslink: 'graph.kindCrosslink',
  contradiction: 'graph.kindContradiction',
}

/**
 * A node in the knowledge graph.
 *
 * Three visual languages, so the shape of the tree is readable at a glance:
 * solid = a note the user wrote, teal = a note an AI pass produced, dashed
 * violet = a proposal that has not been explored yet. A stale note is flagged
 * because the AI's derived knowledge may no longer match it.
 */
function KnowledgeNodeComponent({ data, selected }: NodeProps) {
  const { node } = data as KnowledgeNodeData
  const { t } = useTranslation()

  const isExploration = node.kind === 'exploration'
  const kindKey = node.exploration_kind
    ? EXPLORATION_KIND_KEYS[node.exploration_kind]
    : null

  return (
    <div
      style={{ width: isExploration ? EXPLORATION_NODE_WIDTH : NOTE_NODE_WIDTH }}
      className={cn(
        'rounded-lg border px-3 py-2 text-left shadow-sm transition-colors',
        'bg-card text-card-foreground',
        isExploration
          ? 'border-dashed border-violet-400/70 bg-violet-500/5'
          : node.note_type === 'ai'
            ? 'border-teal-500/50 bg-teal-500/5'
            : 'border-border',
        node.status === 'stale' && 'border-amber-500/70',
        selected && 'ring-2 ring-primary ring-offset-1'
      )}
    >
      <Handle type="target" position={Position.Top} className="!bg-border" />

      <div className="flex items-start gap-1.5">
        {isExploration ? (
          <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-violet-500" />
        ) : (
          <User
            className={cn(
              'mt-0.5 h-3.5 w-3.5 shrink-0',
              node.note_type === 'ai' ? 'text-teal-500' : 'text-muted-foreground'
            )}
          />
        )}
        <span className="line-clamp-2 text-xs font-medium leading-snug">
          {node.label}
        </span>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
        {isExploration ? (
          <>
            {kindKey && (
              <span className="rounded bg-violet-500/10 px-1 py-0.5 text-violet-600 dark:text-violet-300">
                {t(kindKey)}
              </span>
            )}
            {typeof node.score === 'number' && (
              <span>{Math.round(node.score * 100)}%</span>
            )}
          </>
        ) : (
          <>
            <span>
              {node.is_leaf
                ? t('graph.leaf')
                : t('graph.children', { count: node.child_count })}
            </span>
            {node.note_kind && (
              <span className="rounded bg-muted px-1 py-0.5">{node.note_kind}</span>
            )}
            {node.status === 'stale' && (
              <span className="rounded bg-amber-500/15 px-1 py-0.5 text-amber-700 dark:text-amber-300">
                {t('graph.stale')}
              </span>
            )}
          </>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} className="!bg-border" />
    </div>
  )
}

export const KnowledgeNode = memo(KnowledgeNodeComponent)

'use client'

import Link from 'next/link'
import { Bot, ExternalLink, Sparkles, User } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { useTranslation } from '@/lib/hooks/use-translation'
import type { GraphNode } from '@/lib/types/graph'

const EXPLORATION_KIND_KEYS: Record<string, string> = {
  gap: 'graph.kindGap',
  depth: 'graph.kindDepth',
  crosslink: 'graph.kindCrosslink',
  contradiction: 'graph.kindContradiction',
}

interface GraphInspectorProps {
  node: GraphNode | null
  notebookId?: string
  counts?: Record<string, number>
  isExploring: boolean
  isRefining: boolean
  isDismissing: boolean
  onExplore: () => void
  onRefine: () => void
  onDismiss: () => void
}

/**
 * Details and actions for the selected node.
 *
 * With nothing selected it doubles as the legend, so the colour language is
 * always explained without spending a panel on it.
 */
export function GraphInspector({
  node,
  notebookId,
  counts,
  isExploring,
  isRefining,
  isDismissing,
  onExplore,
  onRefine,
  onDismiss,
}: GraphInspectorProps) {
  const { t } = useTranslation()

  if (!node) {
    return (
      <div className="space-y-3 p-4 text-sm">
        <div className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full border border-border" />
          <span className="text-muted-foreground">{t('graph.legendTree')}</span>
          {counts?.notes !== undefined && (
            <span className="ml-auto tabular-nums">{counts.notes}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="h-0.5 w-2.5 bg-blue-400" />
          <span className="text-muted-foreground">{t('graph.legendWeb')}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full border border-dashed border-violet-400" />
          <span className="text-muted-foreground">
            {t('graph.legendExploration')}
          </span>
          {counts?.explorations !== undefined && (
            <span className="ml-auto tabular-nums">{counts.explorations}</span>
          )}
        </div>
      </div>
    )
  }

  const isExploration = node.kind === 'exploration'
  const kindKey = node.exploration_kind
    ? EXPLORATION_KIND_KEYS[node.exploration_kind]
    : null

  return (
    <div className="space-y-3 p-4 text-sm">
      <div className="flex items-start gap-2">
        {isExploration ? (
          <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-violet-500" />
        ) : node.note_type === 'ai' ? (
          <Bot className="mt-0.5 h-4 w-4 shrink-0 text-teal-500" />
        ) : (
          <User className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        )}
        <span className="font-medium leading-snug">{node.label}</span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {isExploration && kindKey && <Badge>{t(kindKey)}</Badge>}
        {isExploration && typeof node.score === 'number' && (
          <Badge variant="outline">{Math.round(node.score * 100)}%</Badge>
        )}
        {!isExploration && node.note_kind && (
          <Badge variant="outline">{node.note_kind}</Badge>
        )}
        {!isExploration && (
          <Badge variant="outline">
            {node.is_leaf
              ? t('graph.leaf')
              : t('graph.children', { count: node.child_count })}
          </Badge>
        )}
        {node.status === 'stale' && <Badge variant="destructive">{t('graph.stale')}</Badge>}
      </div>

      {isExploration && node.rationale && (
        <p className="text-muted-foreground">{node.rationale}</p>
      )}

      {!isExploration && node.tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {node.tags.map((tag) => (
            <span
              key={tag}
              className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      <Separator />

      <div className="space-y-2">
        {isExploration ? (
          <>
            <Button
              className="w-full"
              size="sm"
              onClick={onExplore}
              disabled={isExploring}
            >
              <Sparkles className="mr-1.5 h-3.5 w-3.5" />
              {isExploring ? t('graph.exploring') : t('graph.explore')}
            </Button>
            <Button
              className="w-full"
              size="sm"
              variant="ghost"
              onClick={onDismiss}
              disabled={isDismissing}
            >
              {t('graph.dismiss')}
            </Button>
          </>
        ) : (
          <>
            <Button
              className="w-full"
              size="sm"
              variant="outline"
              onClick={onRefine}
              disabled={isRefining}
            >
              {isRefining ? t('graph.refining') : t('graph.refine')}
            </Button>
            {notebookId && (
              <Button className="w-full" size="sm" variant="ghost" asChild>
                <Link
                  href={`/notebooks/view?id=${notebookId}&modal=note&modalId=${node.id}`}
                >
                  <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
                  {t('graph.openNote')}
                </Link>
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  )
}

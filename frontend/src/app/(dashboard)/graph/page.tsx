'use client'

import { Suspense, useEffect, useMemo, useState } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { Network, RefreshCw, Sparkles } from 'lucide-react'

import { EmptyState } from '@/components/common/EmptyState'
import { LoadingSpinner } from '@/components/common/LoadingSpinner'
import { GraphInspector } from '@/components/graph/GraphInspector'
import { KnowledgeGraphCanvas } from '@/components/graph/KnowledgeGraphCanvas'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  useDismissExploration,
  useExploreKnowledge,
  useKnowledgeGraph,
  useRefineNote,
  useScanFrontier,
} from '@/lib/hooks/use-graph'
import { useNotebooks } from '@/lib/hooks/use-notebooks'
import { useTranslation } from '@/lib/hooks/use-translation'
import { cn } from '@/lib/utils'
import type { GraphNode, KnowledgeGraphResponse } from '@/lib/types/graph'

const ALL_NOTEBOOKS = 'all'

function GraphPageInner() {
  const searchParams = useSearchParams()
  const notebookIdParam = searchParams.get('notebook_id') ?? undefined
  const router = useRouter()
  const pathname = usePathname()
  const { t } = useTranslation()

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showProposals, setShowProposals] = useState(true)

  // A scan is a background job that can run for minutes, and one mutation hook
  // only tracks its latest run - so the pending flag is global. Remembering
  // which notebook was scanned lets the button report "scanning" only where
  // that is true: otherwise switching to another notebook kept showing a
  // disabled, relabelled button and the action looked like it had vanished.
  const [scanNotebookId, setScanNotebookId] = useState<string | null>(null)
  const [scanStartedAt, setScanStartedAt] = useState<number | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)

  useEffect(() => {
    if (scanStartedAt === null) {
      setElapsedSeconds(0)
      return
    }
    const timer = setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - scanStartedAt) / 1000))
    }, 1000)
    return () => clearInterval(timer)
  }, [scanStartedAt])

  const { data: notebooks } = useNotebooks()
  const {
    data: graph,
    isLoading,
    isError,
    refetch,
    isRefetching,
  } = useKnowledgeGraph(notebookIdParam)

  const scan = useScanFrontier()
  const explore = useExploreKnowledge()
  const refine = useRefineNote()
  const dismiss = useDismissExploration(notebookIdParam)

  // The proposals toggle is applied client-side so it is instant: the graph is
  // fetched once with everything in it.
  const visibleGraph: KnowledgeGraphResponse | undefined = useMemo(() => {
    if (!graph) return undefined
    if (showProposals) return graph
    const hidden = new Set(
      graph.nodes.filter((node) => node.kind === 'exploration').map((node) => node.id)
    )
    return {
      ...graph,
      nodes: graph.nodes.filter((node) => !hidden.has(node.id)),
      edges: graph.edges.filter(
        (edge) => !hidden.has(edge.source) && !hidden.has(edge.target)
      ),
    }
  }, [graph, showProposals])

  const selectedNode: GraphNode | null = useMemo(() => {
    if (!selectedId || !graph) return null
    return graph.nodes.find((node) => node.id === selectedId) ?? null
  }, [graph, selectedId])

  const notebookName = useMemo(() => {
    if (!notebookIdParam) return t('graph.allNotebooks')
    return (
      notebooks?.find((notebook) => notebook.id === notebookIdParam)?.name ??
      t('graph.allNotebooks')
    )
  }, [notebooks, notebookIdParam, t])

  const handleExplore = () => {
    if (!selectedNode) return
    explore.mutate({
      anchorNoteId: selectedNode.anchor_id ?? selectedNode.id,
      notebookId: notebookIdParam,
      explorationPointId: selectedNode.id,
    })
  }

  const handleRefine = () => {
    if (!selectedNode) return
    refine.mutate({ noteId: selectedNode.id })
  }

  const handleDismiss = () => {
    if (!selectedNode) return
    dismiss.mutate(selectedNode.id, {
      onSuccess: () => setSelectedId(null),
    })
  }

  const isScanningHere =
    scanNotebookId === notebookIdParam && scanStartedAt !== null

  const handleScan = () => {
    if (!notebookIdParam) return
    const target = notebookIdParam
    setScanNotebookId(target)
    setScanStartedAt(Date.now())
    scan.mutate(
      { notebookId: target },
      {
        onSettled: () => {
          setScanNotebookId(null)
          setScanStartedAt(null)
        },
      }
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Network className="h-4 w-4 text-teal-500" />
          <h1 className="text-sm font-medium">{t('graph.title')}</h1>
        </div>

        <Select
          value={notebookIdParam ?? ALL_NOTEBOOKS}
          onValueChange={(value) => {
            setSelectedId(null)
            // push (not replaceState): the page reads the scope from
            // useSearchParams, which only updates through the router.
            router.push(
              value === ALL_NOTEBOOKS
                ? pathname
                : `${pathname}?notebook_id=${value}`,
              { scroll: false }
            )
          }}
        >
          <SelectTrigger className="h-8 w-56">
            <SelectValue placeholder={notebookName} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_NOTEBOOKS}>{t('graph.allNotebooks')}</SelectItem>
            {(notebooks ?? []).map((notebook) => (
              <SelectItem key={notebook.id} value={notebook.id}>
                {notebook.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <Checkbox
            checked={showProposals}
            onCheckedChange={(checked) => setShowProposals(checked === true)}
          />
          {t('graph.showProposals')}
        </label>

        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => refetch()}
            disabled={isRefetching}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            {t('common.refresh')}
          </Button>
          {/* Scanning needs a notebook to scope the frontier to. */}
          {notebookIdParam && (
            <Button size="sm" onClick={handleScan} disabled={isScanningHere}>
              <Sparkles
                className={cn(
                  'mr-1.5 h-3.5 w-3.5',
                  isScanningHere && 'animate-pulse'
                )}
              />
              {isScanningHere
                ? `${t('graph.scanning')} ${elapsedSeconds}s`
                : t('graph.scanFrontier')}
            </Button>
          )}
        </div>
      </div>

      {graph && (graph.truncated || graph.external_link_count > 0) && (
        <div className="flex flex-wrap gap-4 border-b border-border bg-muted/40 px-4 py-1.5 text-xs text-muted-foreground">
          {graph.truncated && (
            <span>{t('graph.truncated', { count: graph.max_nodes })}</span>
          )}
          {graph.external_link_count > 0 && (
            <span>
              {t('graph.externalLinks', { count: graph.external_link_count })}
            </span>
          )}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          {isLoading ? (
            <div className="flex h-full items-center justify-center">
              <LoadingSpinner />
            </div>
          ) : isError ? (
            <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted-foreground">
              {t('graph.loadFailed')}
            </div>
          ) : !visibleGraph || visibleGraph.nodes.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <EmptyState
                icon={Network}
                title={t('graph.empty')}
                description={t('graph.emptyHint')}
              />
            </div>
          ) : (
            <KnowledgeGraphCanvas
              graph={visibleGraph}
              selectedNodeId={selectedId}
              onSelectNode={setSelectedId}
            />
          )}
        </div>

        <aside className="w-72 shrink-0 overflow-y-auto border-l border-border">
          <GraphInspector
            node={selectedNode}
            notebookId={notebookIdParam}
            counts={graph?.counts}
            isExploring={explore.isPending}
            isRefining={refine.isPending}
            isDismissing={dismiss.isPending}
            onExplore={handleExplore}
            onRefine={handleRefine}
            onDismiss={handleDismiss}
          />
        </aside>
      </div>
    </div>
  )
}

export default function GraphPage() {
  return (
    <AppShell>
      <Suspense
        fallback={
          <div className="flex h-full items-center justify-center">
            <LoadingSpinner />
          </div>
        }
      >
        <GraphPageInner />
      </Suspense>
    </AppShell>
  )
}

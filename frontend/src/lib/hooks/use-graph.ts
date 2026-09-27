'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { explorationsApi, graphApi, iterateApi } from '@/lib/api/graph'
import { waitForJob, type JobOutcome } from '@/lib/api/jobs'
import { QUERY_KEYS } from '@/lib/api/query-client'
import { useToast } from '@/lib/hooks/use-toast'
import { useTranslation } from '@/lib/hooks/use-translation'
import { getApiErrorKey } from '@/lib/utils/error-handler'

/**
 * Explorations always come back with the graph (the toggle is applied in the
 * client), so flipping "show proposals" is instant instead of a round trip.
 */
export function useKnowledgeGraph(notebookId?: string) {
  return useQuery({
    queryKey: QUERY_KEYS.graph(notebookId),
    queryFn: () =>
      graphApi.get({ notebook_id: notebookId, include_explorations: true }),
  })
}

export function useExplorations(notebookId?: string) {
  return useQuery({
    queryKey: QUERY_KEYS.explorations(notebookId),
    queryFn: () => explorationsApi.list({ notebook_id: notebookId }),
  })
}

function invalidateGraph(
  queryClient: ReturnType<typeof useQueryClient>,
  notebookId?: string
) {
  queryClient.invalidateQueries({ queryKey: QUERY_KEYS.graph(notebookId) })
  queryClient.invalidateQueries({ queryKey: QUERY_KEYS.explorations(notebookId) })
}

/** Scan the frontier: propose what to explore next (async job). */
export function useScanFrontier() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: async (vars: { notebookId: string; maxLeaves?: number }) => {
      const job = await explorationsApi.scan({
        notebook_id: vars.notebookId,
        max_leaves: vars.maxLeaves,
      })
      return waitForJob(job.job_id)
    },
    onSuccess: (outcome, vars) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.graph(vars.notebookId) })
      queryClient.invalidateQueries({
        queryKey: QUERY_KEYS.explorations(vars.notebookId),
      })

      if (!outcome.ok) {
        toast({
          title: t('common.error'),
          description: outcome.errorMessage || t('graph.scanFailed'),
          variant: 'destructive',
        })
        return
      }

      const created = Number((outcome.result?.created as number | undefined) ?? 0)
      toast({
        title: t('common.success'),
        description:
          created === 0
            ? t('graph.scanNothingNew')
            : t('graph.scanCreated', { count: created }),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('common.error'),
        description: getApiErrorKey(error, t('graph.scanFailed')),
        variant: 'destructive',
      })
    },
  })
}

/**
 * Answer an exploration point by consolidating its anchor note into a new
 * knowledge note that grows out of it. On success the point is consumed, so it
 * stops being offered once it has produced knowledge.
 */
export function useExploreKnowledge() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: async (vars: {
      anchorNoteId: string
      notebookId?: string
      explorationPointId?: string
      instructions?: string
    }): Promise<JobOutcome> => {
      const job = await iterateApi.synthesize({
        note_ids: [vars.anchorNoteId],
        parent_note_id: vars.anchorNoteId,
        notebook_id: vars.notebookId,
        instructions: vars.instructions,
      })
      const outcome = await waitForJob(job.job_id)

      if (outcome.ok && vars.explorationPointId) {
        try {
          await explorationsApi.consume(vars.explorationPointId)
        } catch {
          // The knowledge exists; failing to retire the proposal must not turn
          // a successful run into an error.
        }
      }
      return outcome
    },
    onSuccess: (outcome, vars) => {
      invalidateGraph(queryClient, vars.notebookId)
      queryClient.invalidateQueries({ queryKey: ['notes'] })
      if (!outcome.ok) {
        toast({
          title: t('common.error'),
          description: outcome.errorMessage || t('graph.exploreFailed'),
          variant: 'destructive',
        })
        return
      }
      toast({ title: t('common.success'), description: t('graph.exploreStarted') })
    },
    onError: (error: unknown) => {
      toast({
        title: t('common.error'),
        description: getApiErrorKey(error, t('graph.exploreFailed')),
        variant: 'destructive',
      })
    },
  })
}

/**
 * Ask the AI to propose a revision of a note. Nothing is written to the note:
 * the result is a proposal the user reviews.
 */
export function useRefineNote() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: async (vars: { noteId: string }): Promise<JobOutcome> => {
      const job = await iterateApi.refine({ note_id: vars.noteId })
      return waitForJob(job.job_id)
    },
    onSuccess: (outcome) => {
      queryClient.invalidateQueries({ queryKey: ['notes'] })
      if (!outcome.ok) {
        toast({
          title: t('common.error'),
          description: outcome.errorMessage || t('graph.refineFailed'),
          variant: 'destructive',
        })
        return
      }
      toast({
        title: t('common.success'),
        description: t('graph.refineProposal'),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('common.error'),
        description: getApiErrorKey(error, t('graph.refineFailed')),
        variant: 'destructive',
      })
    },
  })
}

export function useDismissExploration(notebookId?: string) {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (pointId: string) => explorationsApi.dismiss(pointId),
    onSuccess: () => invalidateGraph(queryClient, notebookId),
    onError: (error: unknown) => {
      toast({
        title: t('common.error'),
        description: getApiErrorKey(error, t('graph.dismissFailed')),
        variant: 'destructive',
      })
    },
  })
}

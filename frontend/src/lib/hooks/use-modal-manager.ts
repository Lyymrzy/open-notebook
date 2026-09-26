'use client'

import { useCallback } from 'react'
import { useRouter, useSearchParams, usePathname } from 'next/navigation'

export type ModalType = 'source' | 'note' | 'insight'

export function useModalManager() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const pathname = usePathname()

  // Read current modal state from URL params.
  //
  // The modal content id uses its own key (`modalId`), NOT `id`: pages that can
  // host a modal keep their own record id in `id` (`/notebooks/view?id=…`,
  // `/sources/view?id=…`) now that the frontend is a static export and route
  // params live in the query string. Sharing the key made opening a modal
  // overwrite the page's id - and closing it delete the id outright.
  const modalType = searchParams?.get('modal') as ModalType | null
  const modalId = searchParams?.get('modalId')

  /**
   * Open a modal by updating URL params without navigation
   * @param type - Type of modal to open (source, note, insight)
   * @param id - ID of the content to display
   */
  // Memoized so consumers can safely use it as a dependency (e.g. useCallback /
  // React.memo props) without re-creating handlers on every render.
  const openModal = useCallback((type: ModalType, id: string) => {
    const params = new URLSearchParams(searchParams?.toString() || '')
    params.set('modal', type)
    params.set('modalId', id)
    // Use scroll: false to prevent page from scrolling when modal state changes
    router.push(`${pathname}?${params.toString()}`, { scroll: false })
  }, [router, searchParams, pathname])

  /**
   * Close the currently open modal by removing modal params from URL
   */
  const closeModal = useCallback(() => {
    const params = new URLSearchParams(searchParams?.toString() || '')
    params.delete('modal')
    params.delete('modalId')
    router.push(`${pathname}?${params.toString()}`, { scroll: false })
  }, [router, searchParams, pathname])

  return {
    modalType,
    modalId,
    openModal,
    closeModal,
    isOpen: !!modalType && !!modalId
  }
}

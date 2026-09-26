/* eslint-disable @typescript-eslint/no-explicit-any */
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useModalManager } from './use-modal-manager'
import { useRouter, useSearchParams, usePathname } from 'next/navigation'

// Mock next/navigation
vi.mock('next/navigation', () => ({
  useRouter: vi.fn(),
  useSearchParams: vi.fn(),
  usePathname: vi.fn(),
}))

describe('useModalManager', () => {
  const pushMock = vi.fn()
  const pathnameMock = '/test-path'
  
  beforeEach(() => {
    vi.mocked(useRouter).mockReturnValue({ push: pushMock } as any)
    vi.mocked(usePathname).mockReturnValue(pathnameMock)
    pushMock.mockClear()
  })

  it('should return null modal state when no params present', () => {
    vi.mocked(useSearchParams).mockReturnValue(new URLSearchParams() as any)
    const { result } = renderHook(() => useModalManager())
    
    expect(result.current.modalType).toBeNull()
    expect(result.current.modalId).toBeNull()
    expect(result.current.isOpen).toBe(false)
  })

  it('should read modal state from URL params', () => {
    vi.mocked(useSearchParams).mockReturnValue(new URLSearchParams('modal=note&modalId=123') as any)
    const { result } = renderHook(() => useModalManager())
    
    expect(result.current.modalType).toBe('note')
    expect(result.current.modalId).toBe('123')
    expect(result.current.isOpen).toBe(true)
  })

  it('should ignore a page-level id param', () => {
    // `/notebooks/view?id=<record>` hosts modals: its own id must not be read
    // as the modal content id.
    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams('id=notebook%3Aabc') as any
    )
    const { result } = renderHook(() => useModalManager())

    expect(result.current.modalType).toBeNull()
    expect(result.current.modalId).toBeNull()
    expect(result.current.isOpen).toBe(false)
  })

  it('should call router.push when opening a modal', () => {
    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams('id=notebook%3Aabc') as any
    )
    const { result } = renderHook(() => useModalManager())
    
    act(() => {
      result.current.openModal('source', 'abc')
    })
    
    expect(pushMock).toHaveBeenCalledWith(
      '/test-path?id=notebook%3Aabc&modal=source&modalId=abc',
      { scroll: false }
    )
  })

  it('should keep the page id when closing a modal', () => {
    vi.mocked(useSearchParams).mockReturnValue(
      new URLSearchParams('id=notebook%3Aabc&modal=note&modalId=123') as any
    )
    const { result } = renderHook(() => useModalManager())
    
    act(() => {
      result.current.closeModal()
    })
    
    expect(pushMock).toHaveBeenCalledWith('/test-path?id=notebook%3Aabc', {
      scroll: false,
    })
  })
})

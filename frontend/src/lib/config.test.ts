import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { getApiUrl, resetConfig } from './config'

describe('Config Priority', () => {
  const originalEnv = process.env
  const originalFetch = global.fetch
  const fetchMock = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    resetConfig()
    process.env = { ...originalEnv }
    fetchMock.mockReset()
    global.fetch = fetchMock
  })

  afterEach(() => {
    process.env = originalEnv
    global.fetch = originalFetch
  })

  it('should use the build-time env var when set', async () => {
    // The static export has no server-side `/config` endpoint: the API base is
    // the env var (or same-origin ''), and only `/api/config` is fetched.
    process.env.NEXT_PUBLIC_API_URL = 'http://env-url.com'

    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ version: '1.0.0' }),
    } as Response)

    const url = await getApiUrl()
    expect(url).toBe('http://env-url.com')
  })

  it('should use the env var even when the backend config response has no apiUrl', async () => {
    // Setup: Env var set, Runtime config returns empty string (simulating not set)
    process.env.NEXT_PUBLIC_API_URL = 'http://env-url.com'
    
    // First fetch: /config returns empty apiUrl
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ apiUrl: '' }),
    } as Response)

    // Second fetch: api/config check using env url
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ version: '1.0.0' }),
    } as Response)

    const url = await getApiUrl()
    expect(url).toBe('http://env-url.com')
  })

  it('should use the env var when the backend config response is an empty object', async () => {
    // Setup: Env var set, Runtime config returns empty object
    process.env.NEXT_PUBLIC_API_URL = 'http://env-url.com'
    
    // First fetch: /config returns {}
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({}), // Missing apiUrl
    } as Response)

    // Second fetch: api/config check using env url
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ version: '1.0.0' }),
    } as Response)

    const url = await getApiUrl()
    expect(url).toBe('http://env-url.com')
  })

  it('should use same-origin (relative path) when the env var is missing', async () => {
    // Setup: Env var NOT set, Runtime config returns empty
    delete process.env.NEXT_PUBLIC_API_URL
    
    // First fetch: /config returns empty
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ apiUrl: '' }),
    } as Response)

    // Second fetch: api/config check using default relative path
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ version: '1.0.0' }),
    } as Response)

    const url = await getApiUrl()
    expect(url).toBe('')
  })
})

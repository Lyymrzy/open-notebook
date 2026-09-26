'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

// A static export cannot run the server `redirect()` this page used to rely on.
// The FastAPI host also redirects `/` → `/notebooks`; this client fallback keeps
// the root usable when the exported files are opened directly (e.g. `next dev`).
export default function HomePage() {
  const router = useRouter()

  useEffect(() => {
    router.replace('/notebooks')
  }, [router])

  return null
}

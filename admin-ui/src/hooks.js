import { useCallback, useEffect, useRef, useState } from 'react'
import { AuthError } from './api.js'
import { useAuth } from './auth.jsx'

// Polling resiliente: mantém o último estado bom, marca lastOk/error, e
// desloga em 401. `intervalMs=0` desliga o auto-refresh (busca só 1x).
// `enabled=false` pausa sem perder os dados já carregados.
export function usePoll(fetcher, deps = [], { intervalMs = 0, enabled = true } = {}) {
  const { onUnauthorized } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [lastOk, setLastOk] = useState(null)
  const [loading, setLoading] = useState(true)
  const fnRef = useRef(fetcher)
  fnRef.current = fetcher

  const run = useCallback(async () => {
    try {
      const out = await fnRef.current()
      setData(out)
      setError(null)
      setLastOk(Date.now())
    } catch (e) {
      if (e instanceof AuthError) { onUnauthorized(); return }
      setError(e.message || 'erro')
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onUnauthorized])

  useEffect(() => {
    setLoading(true)
    run()
    if (!enabled || !intervalMs) return
    const t = setInterval(run, intervalMs)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs, enabled])

  return { data, error, lastOk, loading, refresh: run }
}

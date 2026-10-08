import { useEffect, useRef, useState } from 'react'

// Polling com rastreio de frescor: além do dado, devolve `error` do último
// ciclo e `lastOk` (quando o backend respondeu pela última vez). Permite mostrar
// "atualizado há Xs" e avisar SEM apagar o último estado bom quando o poll falha.
export function usePolling(fetcher, ms = 5000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [lastOk, setLastOk] = useState(null)
  const ref = useRef(fetcher)
  ref.current = fetcher

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = await ref.current()
        if (!alive) return
        setData(d); setError(null); setLastOk(Date.now())
      } catch (e) {
        if (alive) setError(e)  // mantém `data` anterior (não pisca p/ vazio)
      }
    }
    tick()
    const t = setInterval(tick, ms)
    return () => { alive = false; clearInterval(t) }
  }, [ms])

  return { data, error, lastOk }
}

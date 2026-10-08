import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDownToLine, Pause, Play } from 'lucide-react'
import { api } from '../api.js'
import { usePoll } from '../hooks.js'
import { Freshness } from './ui.jsx'

const levelCls = {
  error: 'text-red-600 dark:text-red-400',
  warn: 'text-amber-600 dark:text-amber-400',
  info: 'text-neutral-500 dark:text-neutral-400',
}

function fmtTs(ts) {
  const d = new Date(typeof ts === 'number' ? ts : Date.parse(ts))
  if (Number.isNaN(d.getTime())) return '--:--:--'
  return d.toLocaleTimeString('pt-BR', { hour12: false })
}

// Visualizador de log com follow (auto-scroll) e auto-refresh por polling.
export default function LogViewer({ instanceId, runId, live }) {
  // `live` (run em andamento) liga o polling por padrão.
  const [follow, setFollow] = useState(true)
  const [auto, setAuto] = useState(!!live)
  const boxRef = useRef(null)

  const { data, error, lastOk } = usePoll(
    () => api.getRunLog(instanceId, runId),
    [instanceId, runId],
    { intervalMs: auto ? 2000 : 0 },
  )
  const lines = data?.lines || []

  // Auto-scroll ao fim quando follow está ligado e chegam linhas novas.
  useLayoutEffect(() => {
    if (follow && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight
  }, [lines.length, follow])

  // Se o usuário rolar p/ cima, desliga o follow; ao voltar ao fim, religa.
  const onScroll = () => {
    const el = boxRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    setFollow(atBottom)
  }

  return (
    <div className="rounded-lg border border-neutral-200 dark:border-neutral-800 overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-3 py-1.5 border-b border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-900/60">
        <span className="text-xs font-medium text-neutral-500">Log · {runId}</span>
        <div className="flex items-center gap-3">
          <Freshness lastOk={lastOk} error={error} />
          <button
            type="button"
            onClick={() => setAuto((a) => !a)}
            className="inline-flex items-center gap-1 text-xs text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-200 transition"
            title={auto ? 'Pausar auto-refresh' : 'Ligar auto-refresh'}
          >
            {auto ? <Pause size={13} /> : <Play size={13} />}
            {auto ? 'auto 2s' : 'pausado'}
          </button>
          <button
            type="button"
            onClick={() => { setFollow(true); if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight }}
            className={`inline-flex items-center gap-1 text-xs transition ${follow ? 'text-lime-600 dark:text-lime-400' : 'text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-200'}`}
            title="Seguir o fim do log"
          >
            <ArrowDownToLine size={13} />
            follow
          </button>
        </div>
      </div>

      <div ref={boxRef} onScroll={onScroll} className="h-72 overflow-auto bg-neutral-950 px-3 py-2 font-mono text-[12px] leading-relaxed">
        {lines.length === 0 && <p className="text-neutral-600">sem linhas.</p>}
        {lines.map((l, i) => (
          <div key={i} className="flex gap-2 whitespace-pre-wrap break-words">
            <span className="text-neutral-600 shrink-0 tabular-nums">{fmtTs(l.ts)}</span>
            <span className={`shrink-0 uppercase ${levelCls[l.level] || levelCls.info}`}>{(l.level || 'info').padEnd(5)}</span>
            <span className="text-neutral-200">{l.msg}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

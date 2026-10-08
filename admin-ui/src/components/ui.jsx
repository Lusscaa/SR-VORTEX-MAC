// Primitivos visuais — mesmo espírito do painel de operação (frontend/):
// tema neutro preto/branco, denso, dark mode por classe, lime só p/ sinal.
import { useEffect, useState } from 'react'
import { Check, Copy, Eye, EyeOff } from 'lucide-react'
import { timeAgo } from '../utils.js'

const focusRing =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60 ' +
  'focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-neutral-900'

export const btnPrimary =
  'rounded-lg bg-neutral-900 text-white hover:bg-neutral-700 ' +
  'dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200 ' +
  'font-medium transition disabled:opacity-40 disabled:cursor-not-allowed ' + focusRing

export const btnGhost =
  'rounded-lg border border-neutral-300 dark:border-neutral-700 ' +
  'text-neutral-700 dark:text-neutral-200 hover:bg-neutral-100 ' +
  'dark:hover:bg-neutral-800 font-medium transition ' + focusRing

export const inputCls =
  'w-full rounded-lg border border-neutral-300 dark:border-neutral-700 ' +
  'bg-transparent px-3 py-2 outline-none focus:border-neutral-900 ' +
  'dark:focus:border-neutral-100 ' + focusRing

// "ao vivo · há Xs" verde; vira âmbar "reconectando…" sem apagar o último OK.
export function Freshness({ lastOk, error }) {
  const [, force] = useState(0)
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [])
  if (!lastOk && !error) return null
  const stale = !!error
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span className={`h-1.5 w-1.5 rounded-full ${stale ? 'bg-amber-500' : 'bg-lime-500 animate-pulse'}`} />
      <span className={stale ? 'text-amber-600 dark:text-amber-400' : 'text-neutral-400'}>
        {stale ? 'reconectando…' : 'ao vivo'}
        {lastOk && <span className="text-neutral-400"> · {timeAgo(lastOk)}</span>}
      </span>
    </span>
  )
}

export function CopyButton({ text, title = 'Copiar', className = '' }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text || '')
      setDone(true)
      setTimeout(() => setDone(false), 1500)
    } catch { /* clipboard bloqueado */ }
  }
  return (
    <button
      type="button"
      onClick={copy}
      title={title}
      aria-label={done ? 'Copiado' : title}
      className={`inline-flex items-center gap-1 text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200 transition ${className}`}
    >
      {done ? <Check size={13} className="text-lime-600 dark:text-lime-400" /> : <Copy size={13} />}
    </button>
  )
}

// Senha mascarada com botão de revelar + copiar.
export function Secret({ value }) {
  const [show, setShow] = useState(false)
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-xs">
      <span className="tabular-nums">{show ? value : '•'.repeat(Math.min(10, (value || '').length || 6))}</span>
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? 'Ocultar' : 'Revelar'}
        className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200 transition"
      >
        {show ? <EyeOff size={13} /> : <Eye size={13} />}
      </button>
      <CopyButton text={value} />
    </span>
  )
}

export function PageHead({ title, subtitle, children }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-bold tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-neutral-500 dark:text-neutral-400">{subtitle}</p>}
      </div>
      {children}
    </div>
  )
}

export function Skeleton({ className = '' }) {
  return <div className={`animate-pulse rounded-md bg-neutral-200/70 dark:bg-neutral-800/70 ${className}`} />
}

export function Card({ className = '', children, onClick }) {
  const clickable = onClick
    ? 'cursor-pointer hover:border-neutral-300 dark:hover:border-neutral-700 hover:shadow-sm transition'
    : ''
  return (
    <div
      onClick={onClick}
      className={`rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-4 ${clickable} ${className}`}
    >
      {children}
    </div>
  )
}

export function Stat({ label, value, hint }) {
  return (
    <div className="rounded-lg border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-900/60 px-3 py-2">
      <p className="text-[11px] font-medium text-neutral-500 dark:text-neutral-400">{label}</p>
      <p className="mt-0.5 text-lg font-bold tabular-nums">{value}</p>
      {hint && <p className="text-[11px] text-neutral-400">{hint}</p>}
    </div>
  )
}

export function Badge({ tone = 'neutral', children }) {
  const tones = {
    green: 'bg-lime-100 text-lime-700 dark:bg-lime-900/40 dark:text-lime-300',
    danger: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
    amber: 'bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300',
    running: 'bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300',
    neutral: 'bg-neutral-100 text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400',
  }
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold ${tones[tone] || tones.neutral}`}>
      {tone === 'running' && <span className="h-1.5 w-1.5 rounded-full bg-neutral-500 dark:bg-neutral-400 animate-pulse" />}
      {children}
    </span>
  )
}

// Mapeia status de run -> tom do Badge. Alarme (vermelho) p/ falha;
// âmbar p/ incompleto (acionável); lime p/ sucesso; resto quieto.
export function statusTone(status) {
  switch (status) {
    case 'success': return 'green'
    case 'failed': case 'error': return 'danger'
    case 'incomplete': return 'amber'
    case 'running': return 'running'
    default: return 'neutral'
  }
}

export function Table({ columns, rows, empty = 'Nada por aqui ainda.', onRowClick, rowKey }) {
  if (!rows?.length) return <p className="text-sm text-neutral-400 py-8 text-center">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-neutral-400 border-b border-neutral-200 dark:border-neutral-800">
            {columns.map((c) => (
              <th key={c.key} className="font-medium px-3 py-2 text-xs uppercase tracking-wide">{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={rowKey ? rowKey(r) : i}
              onClick={onRowClick ? () => onRowClick(r) : undefined}
              className={`border-b border-neutral-100 dark:border-neutral-800/60 hover:bg-neutral-50 dark:hover:bg-neutral-800/40 ${onRowClick ? 'cursor-pointer' : ''}`}
            >
              {columns.map((c) => (
                <td key={c.key} className="px-3 py-2">{c.render ? c.render(r) : (r[c.key] ?? '—')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

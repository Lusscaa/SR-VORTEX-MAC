// Primitivos visuais — tema NEUTRO (preto/branco), denso, com dark mode.
import { useEffect, useState } from 'react'
import { timeAgo } from '../utils.js'

// Indicador de frescor: "ao vivo · há Xs" em verde; vira âmbar e avisa
// "reconectando…" quando o polling falha, SEM apagar o último estado bom.
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

// Botão de copiar com feedback visível ("Copiado!") por ~1.5s.
export function CopyButton({ text, label = 'Copiar', className = '' }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text || '')
      setDone(true)
      setTimeout(() => setDone(false), 1500)
    } catch { /* clipboard bloqueado */ }
  }
  return (
    <button type="button" onClick={copy} aria-label={done ? 'Copiado' : label} className={className}>
      {done ? '✓ Copiado!' : label}
    </button>
  )
}

// Anel de foco visível p/ teclado (a11y) — sutil, na cor da marca.
const focusRing =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60 ' +
  'focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-neutral-900'

// Botão primário — dourado→verde neon (cyberpunk), consistente em toda a UI.
export const btnPrimary =
  'rounded-lg cyber-btn transition disabled:cursor-not-allowed ' + focusRing

export const btnGhost =
  'rounded-lg border border-neutral-300 dark:border-emerald-400/25 ' +
  'text-neutral-700 dark:text-emerald-100 hover:bg-neutral-100 ' +
  'dark:hover:bg-emerald-400/10 font-medium transition ' + focusRing

export const inputCls =
  'w-full rounded-lg border border-neutral-300 dark:border-emerald-400/25 ' +
  'bg-transparent dark:bg-[#07130d]/50 dark:text-emerald-50 px-3 py-2 outline-none ' +
  'focus:border-neutral-900 dark:focus:border-amber-300/70 ' +
  'dark:placeholder:text-emerald-200/30 ' + focusRing

// Chevrons »»» empilhados (decoração HUD da referência samurai-mecha).
export function HudChevrons({ className = '' }) {
  return (
    <span className={`hud-chevrons hidden dark:inline-flex ${className}`} aria-hidden="true">
      <span>»</span><span>»</span><span>»</span>
    </span>
  )
}

// Barras de áudio animadas (waveform do rodapé da referência).
export function Waveform({ bars = 9, className = '' }) {
  return (
    <span className={`hud-wave hidden dark:flex ${className}`} aria-hidden="true">
      {Array.from({ length: bars }).map((_, i) => <i key={i} style={{ animationDelay: `${i * 0.08}s` }} />)}
    </span>
  )
}

// Linha de "terminal" decorativa (root@vortex:~$ …), só no dark.
export function HudCode({ children, className = '' }) {
  return <div className={`hud-code hidden dark:block ${className}`} aria-hidden="true">{children}</div>
}

export function PageHead({ title, subtitle, children }) {
  return (
    <div className="mb-5">
      <div className="hud-ticks mb-2 hidden dark:flex" />
      <div className="flex items-end justify-between gap-4">
        <div className="flex items-start gap-3">
          <HudChevrons className="mt-1.5 text-[15px]" />
          <div>
            <div className="hud-tag hidden dark:flex items-center gap-2 mb-1">
              <span>// SYSTEM · BOT-SR.VORTEX</span>
              <span className="text-sky-300/50">root@vortex:~$</span>
            </div>
            <h1 className="font-display text-2xl font-extrabold uppercase tracking-wider text-neutral-900 cyber-gold">{title}</h1>
            {subtitle && <p className="text-xs text-neutral-500 dark:text-emerald-200/60 uppercase tracking-wide mt-0.5">{subtitle}</p>}
          </div>
        </div>
        {children}
      </div>
    </div>
  )
}

// Bloco de carregamento — evita o salto de layout do texto "Carregando…".
export function Skeleton({ className = '' }) {
  return <div className={`animate-pulse rounded-md bg-neutral-200/70 dark:bg-neutral-800/70 ${className}`} />
}

// Faixa horizontal de proporção + legenda. Substitui o gráfico de barras (que
// desperdiçava espaço e quebrava de escala). `segments`: [{label, value, bar, dot}].
export function DistributionBar({ segments }) {
  const total = segments.reduce((s, x) => s + x.value, 0)
  const live = segments.filter((s) => s.value > 0)
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800">
        {total === 0
          ? null
          : live.map((s) => (
              <div key={s.label} className={s.bar} style={{ width: `${(s.value / total) * 100}%` }} title={`${s.label}: ${s.value}`} />
            ))}
      </div>
      <div className="mt-3 grid gap-1.5">
        {segments.map((s) => (
          <div key={s.label} className="flex items-center gap-2 text-xs">
            <span className={`h-2 w-2 rounded-full shrink-0 ${s.dot}`} />
            <span className="flex-1 text-neutral-500 dark:text-neutral-400">{s.label}</span>
            <span className="tabular-nums font-semibold">{s.value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function Card({ className = '', children }) {
  return (
    <div className={`cyber-card relative rounded-2xl border border-neutral-200 bg-white p-4 ${className}`}>
      <span className="hud-corner tl" /><span className="hud-corner tr" />
      <span className="hud-corner bl" /><span className="hud-corner br" />
      {children}
    </div>
  )
}

export function Stat({ label, value, hint, accent, positive, danger }) {
  // Número colorido só p/ sinal: 'positive' (lime) e 'danger' (vermelho, falhas
  // que precisam de atenção). 'accent' = card invertido (destaque de total).
  const numCls = accent ? '' : positive ? 'text-lime-600 dark:text-lime-400'
    : (danger && Number(value) > 0) ? 'text-red-600 dark:text-red-400' : ''
  return (
    <div className={`rounded-2xl border p-4 ${accent
      ? 'bg-neutral-900 text-white border-neutral-900 dark:border-transparent dark:text-[#06130d] dark:[background:linear-gradient(95deg,#f4d06a,#e8c14a_45%,#34d399)] dark:shadow-[0_0_22px_-6px_rgba(52,211,153,0.6)]'
      : 'cyber-card border-neutral-200 bg-white'}`}>
      <p className={`text-xs font-medium ${accent ? 'opacity-70' : 'text-neutral-500 dark:text-neutral-400'}`}>{label}</p>
      <p className={`mt-1 text-2xl font-bold tabular-nums ${numCls}`}>{value}</p>
      {hint && <p className={`mt-0.5 text-xs ${accent ? 'opacity-60' : 'text-neutral-400'}`}>{hint}</p>}
    </div>
  )
}

export function Badge({ tone = 'neutral', children }) {
  // Disciplina: tudo neutro, EXCETO estados de alarme. 'danger' (falha/erro/
  // rejeitada) e 'amber' (incompleto = acionável) ganham cor real para o
  // operador escanear num relance. 'warn' (BM em análise) e 'running'/'queued'
  // ficam quietos — não exigem ação imediata.
  const tones = {
    green: 'bg-lime-100 text-lime-700 dark:bg-emerald-400/15 dark:text-emerald-300 dark:ring-1 dark:ring-inset dark:ring-emerald-400/30',
    // ALARME: falhou / rejeitada / erro duro. Vermelho saturado, salta na lista.
    danger: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
    // alias legado: 'red' ainda usado em alguns lugares → mesmo alarme.
    red: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
    // ALARME médio: incompleto (rodou, parou no meio — alguém precisa olhar).
    amber: 'bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300',
    // quieto: 'em análise' / 'a checar' — não acionável agora.
    warn: 'bg-neutral-100 text-neutral-400 dark:bg-neutral-800 dark:text-neutral-500',
    // rodando: neutro com ponto pulsante (sinal de "vivo" sem virar alarme).
    running: 'bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300',
    sky: 'bg-sky-100 text-sky-700 dark:bg-sky-950/50 dark:text-sky-300',
    neutral: 'bg-neutral-100 text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400',
  }
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold ${tones[tone] || tones.neutral}`}>
      {tone === 'running' && <span className="h-1.5 w-1.5 rounded-full bg-neutral-500 dark:bg-neutral-400 animate-pulse" />}
      {tone === 'sky' && <span className="h-1.5 w-1.5 rounded-full bg-sky-400 dark:bg-sky-400 animate-pulse" />}
      {children}
    </span>
  )
}

export function Table({ columns, rows, empty = 'Nada por aqui ainda.' }) {
  if (!rows?.length) return <p className="text-sm text-neutral-400 py-6 text-center">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-neutral-400 border-b border-neutral-200 dark:border-neutral-800">
            {columns.map((c) => <th key={c.key} className="font-medium px-3 py-2 text-xs uppercase tracking-wide">{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-neutral-100 dark:border-neutral-800/60 hover:bg-neutral-50 dark:hover:bg-neutral-800/40">
              {columns.map((c) => <td key={c.key} className="px-3 py-2">{c.render ? c.render(r) : (r[c.key] ?? '—')}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

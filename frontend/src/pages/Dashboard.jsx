import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Activity, CheckCircle2, AlertTriangle, Zap, ShieldCheck, BarChart3, ArrowRight, Calendar, RotateCw, Power, Clock } from "lucide-react"
import { api } from "../api.js"
import { usePolling } from "../hooks.js"
import { Card, DistributionBar, Freshness, PageHead, Skeleton } from "../components/ui.jsx"
import RunsPanel from "../components/RunsPanel.jsx"

// Botão "Reiniciar backend" com confirmação de 2 passos (evita clique acidental).
// Reinicia o processo (recarrega código + .env), espera o /health voltar e
// recarrega o painel. Aviso: runs em andamento param.
function RestartButton() {
  const [phase, setPhase] = useState('idle') // idle | confirm | restarting | done | fail
  const waitForBackend = async () => {
    const deadline = Date.now() + 60000
    await new Promise((r) => setTimeout(r, 2500))
    while (Date.now() < deadline) {
      try { await api.health(); return true }
      catch { await new Promise((r) => setTimeout(r, 1500)) }
    }
    return false
  }
  const doRestart = async () => {
    setPhase('restarting')
    try { await api.restartBackend() } catch { /* a conexão cai antes de responder */ }
    const ok = await waitForBackend()
    if (ok) { setPhase('done'); setTimeout(() => window.location.reload(), 1000) }
    else setPhase('fail')
  }
  if (phase === 'restarting')
    return <span className="inline-flex items-center gap-2 text-xs font-semibold text-amber-600 dark:text-amber-300"><RotateCw size={14} className="animate-spin" /> Reiniciando…</span>
  if (phase === 'done')
    return <span className="inline-flex items-center gap-2 text-xs font-semibold text-emerald-600 dark:text-emerald-300"><CheckCircle2 size={14} /> Voltou! Atualizando…</span>
  if (phase === 'fail')
    return (
      <span className="inline-flex items-center gap-2 text-xs font-semibold text-rose-600 dark:text-rose-300">
        <AlertTriangle size={14} /> Não voltou em 60s — veja a janela do backend
        <button onClick={() => setPhase('idle')} className="underline">ok</button>
      </span>
    )
  if (phase === 'confirm')
    return (
      <span className="inline-flex items-center gap-2">
        <span className="text-xs font-semibold text-amber-600 dark:text-amber-300">Reiniciar? (runs em andamento param)</span>
        <button onClick={doRestart} className="inline-flex items-center gap-1 rounded-lg border border-rose-300 dark:border-rose-400/50 text-rose-700 dark:text-rose-300 px-2.5 py-1 text-xs font-bold uppercase tracking-wide hover:bg-rose-50 dark:hover:bg-rose-400/10">
          <Power size={12} /> Sim, reiniciar
        </button>
        <button onClick={() => setPhase('idle')} className="rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-2.5 py-1 text-xs font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10">
          Cancelar
        </button>
      </span>
    )
  return (
    <button
      type="button" onClick={() => setPhase('confirm')}
      title="Reinicia o backend para aplicar mudanças de código/.env"
      className="inline-flex items-center gap-1.5 rounded-lg border border-neutral-300 dark:border-emerald-400/30 text-neutral-600 dark:text-emerald-200 px-3 py-1.5 text-xs font-bold uppercase tracking-wide hover:bg-neutral-100 dark:hover:bg-emerald-400/10 transition"
    >
      <RotateCw size={13} /> Reiniciar backend
    </button>
  )
}

// KPI HUD — ícone com glow neon + número gigante em Orbitron (cyber-kpi).
const KPI_TONE = {
  blue: 'text-sky-600 dark:text-sky-300',
  green: 'text-emerald-600 dark:text-emerald-300',
  gold: 'text-amber-600 dark:text-amber-300',
  red: 'text-rose-600 dark:text-rose-300',
}
function Kpi({ tone, icon: Icon, code, label, value, hint }) {
  return (
    <Card className="relative overflow-hidden">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="hud-tag hidden dark:block mb-1">{code}</p>
          <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-neutral-500 dark:text-emerald-200/60">{label}</p>
          <p className={`font-display cyber-kpi mt-1.5 text-[2.6rem] leading-none font-black tabular-nums ${KPI_TONE[tone]}`}>{value}</p>
          <p className="mt-2 text-[11px] uppercase tracking-wide text-neutral-400 dark:text-emerald-200/40">{hint}</p>
        </div>
        <span className={`cyber-ibadge ${tone === 'blue' ? 'blue' : tone === 'green' ? 'green' : tone === 'gold' ? 'gold' : 'red'} h-12 w-12 shrink-0`}>
          <Icon size={22} />
        </span>
      </div>
    </Card>
  )
}

export default function Dashboard() {
  const [period, setPeriod] = useState('total')
  const [specificDate, setSpecificDate] = useState('')
  const [smsStatus, setSmsStatus] = useState(null)
  const { data: stats, error: statsErr, lastOk } = usePolling(() => {
    if (specificDate) {
      return api.stats('total', specificDate)
    }
    return api.stats(period)
  }, 5000)

  useEffect(() => {
    api.smsSessionStatus().then(setSmsStatus).catch(() => {})
  }, [])

  // Só derruba a página se NUNCA carregou (boot com backend off). Depois disso,
  // uma falha de poll mantém o último estado bom e o frescor avisa "reconectando".
  if (statsErr && !stats) return <p className="text-red-500 text-sm">Backend indisponível: {statsErr.message}. Rode <code className="font-mono">uvicorn api_server:app --port 8000</code>.</p>
  if (!stats) {
    return (
      <>
        <PageHead title="Início" subtitle="Operação e monitoramento" />
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[120px]" />)}
        </div>
        <Skeleton className="mt-3 h-40" />
        <Skeleton className="mt-3 h-[60vh]" />
      </>
    )
  }

  const r = stats.runs, b = stats.bms
  const segments = [
    { label: '100% concluídos', value: r.success || 0, bar: 'bg-lime-500', dot: 'bg-lime-500' },
    { label: 'Incompletos', value: r.incomplete || 0, bar: 'bg-amber-500', dot: 'bg-amber-500' },
    { label: 'Falhas', value: r.failed || 0, bar: 'bg-red-500', dot: 'bg-red-500' },
    { label: 'Rodando', value: r.running || 0, bar: 'bg-neutral-400', dot: 'bg-neutral-400' },
    { label: 'Na fila', value: r.queued || 0, bar: 'bg-neutral-300 dark:bg-neutral-600', dot: 'bg-neutral-300 dark:bg-neutral-600' },
  ]

  const periodOptions = [
    { label: 'Total', value: 'total' },
    { label: 'Hoje', value: 'dia' },
    { label: 'Semana', value: 'semana' },
    { label: 'Mês', value: 'mês' },
  ]

  return (
    <>
      <PageHead title="Início" subtitle="Operação e monitoramento">
        <div className="flex items-center gap-3">
          <Freshness lastOk={lastOk} error={statsErr} />
          <RestartButton />
        </div>
      </PageHead>

      {/* Barra de período — estilo HUD */}
      <Card className="mb-6 flex flex-wrap items-center gap-x-4 gap-y-3">
        <span className="hud-tag hidden dark:inline">// PERÍODO</span>
        <span className="text-[11px] font-bold uppercase tracking-wider text-neutral-500 dark:hidden">Período</span>
        <div className="flex gap-1.5 flex-wrap">
          {periodOptions.map((opt) => {
            const active = !specificDate && period === opt.value
            return (
              <button
                key={opt.value}
                onClick={() => { setPeriod(opt.value); setSpecificDate('') }}
                className={`px-4 py-1.5 text-xs font-bold uppercase tracking-wide rounded-lg transition-all ${
                  active
                    ? 'cyber-btn'
                    : 'border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 hover:bg-neutral-100 dark:hover:bg-emerald-400/10'
                }`}
              >
                {opt.label}
              </button>
            )
          })}
        </div>
        <div className="flex items-center gap-2 border-l border-neutral-200 dark:border-emerald-400/20 pl-3 ml-auto">
          <Calendar size={15} className="text-neutral-400 dark:text-emerald-300/60" />
          <input
            type="date"
            value={specificDate}
            onChange={(e) => setSpecificDate(e.target.value)}
            className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
              specificDate
                ? 'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/30 dark:border-emerald-500 text-neutral-900 dark:text-emerald-100 font-semibold'
                : 'border-neutral-300 dark:border-emerald-400/25 bg-transparent dark:bg-[#07130d]/50 text-neutral-900 dark:text-emerald-100'
            }`}
          />
          {specificDate && (
            <button onClick={() => setSpecificDate('')} className="text-rose-500 hover:text-rose-600 font-bold text-lg leading-none">✕</button>
          )}
        </div>
      </Card>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <Kpi tone="blue"  icon={Activity}     code="// RUN·TOTAL" label="Runs Total"     value={(r.total || 0) - (r.failed || 0)} hint={`${stats.jobs.active} ativo(s)`} />
        <Kpi tone="green" icon={CheckCircle2}  code="// OK·100"     label="100% Concluídos" value={r.success || 0}   hint="fluxo completo" />
        <Kpi tone="gold"  icon={Zap}           code="// HALF"       label="Incompletos"    value={r.incomplete || 0} hint="pararam no meio" />
        <Kpi tone="red"   icon={AlertTriangle} code="// ERR·404"    label="Falhas"         value={r.failed || 0}     hint="não passaram no boot" />
      </div>

      {/* Alertas operacionais */}
      {smsStatus?.warning && (
        <div className={`mb-4 p-3 rounded-lg border-l-4 flex items-center gap-3 ${
          smsStatus.valid === false
            ? 'border-rose-500 bg-rose-50 dark:bg-rose-950/30 text-rose-700 dark:text-rose-300'
            : 'border-amber-500 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300'
        }`}>
          <AlertTriangle size={18} className="shrink-0" />
          <p className="text-sm font-semibold">SMS Session: {smsStatus.warning}</p>
        </div>
      )}

      {stats.projection && (
        <div className="mb-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
          {stats.projection.accounts_hours_left != null && (
            <div className={`p-3 rounded-lg border-l-4 flex items-center gap-3 ${
              stats.projection.accounts_hours_left < 12
                ? 'border-rose-500 bg-rose-50 dark:bg-rose-950/30 text-rose-700 dark:text-rose-300'
                : stats.projection.accounts_hours_left < 48
                ? 'border-amber-500 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300'
                : 'border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300'
            }`}>
              <Clock size={18} className="shrink-0" />
              <p className="text-sm font-semibold">
                Contas: ~{stats.projection.accounts_hours_left}h restantes
                <span className="font-normal opacity-70"> ({stats.projection.success_24h} runs/24h)</span>
              </p>
            </div>
          )}
          {stats.projection.cnpjs_hours_left != null && (
            <div className={`p-3 rounded-lg border-l-4 flex items-center gap-3 ${
              stats.projection.cnpjs_hours_left < 12
                ? 'border-rose-500 bg-rose-50 dark:bg-rose-950/30 text-rose-700 dark:text-rose-300'
                : stats.projection.cnpjs_hours_left < 48
                ? 'border-amber-500 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300'
                : 'border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300'
            }`}>
              <Clock size={18} className="shrink-0" />
              <p className="text-sm font-semibold">
                CNPJs: ~{stats.projection.cnpjs_hours_left}h restantes
                <span className="font-normal opacity-70"> ({stats.projection.success_24h} runs/24h)</span>
              </p>
            </div>
          )}
        </div>
      )}

      {/* Runs em Tempo Real */}
      <div className="mt-6">
        <div className="flex items-center gap-2.5 mb-4">
          <span className="cyber-ibadge green h-8 w-8"><Activity size={16} /></span>
          <h2 className="font-display text-lg font-extrabold uppercase tracking-wider text-neutral-900 dark:text-emerald-100">Runs em Tempo Real</h2>
          <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide bg-emerald-100 dark:bg-emerald-400/10 text-emerald-700 dark:text-emerald-300 px-3 py-1 rounded-full ring-1 ring-inset ring-emerald-400/30">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" /> Ao vivo
          </span>
        </div>
        <RunsPanel variant="page" />
      </div>

      {/* BM 2K (herói) + Distribuição / Falhas */}
      <div className="mt-6 grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="flex flex-col overflow-hidden">
          <div className="flex items-start justify-between gap-2 mb-4">
            <div>
              <p className="hud-tag hidden dark:block mb-1">// BUSINESS MANAGERS</p>
              <span className="cyber-ibadge violet h-9 w-9 inline-grid mb-2"><ShieldCheck size={18} /></span>
            </div>
            <Link to="/bms" className="inline-flex items-center gap-1 text-[11px] font-bold uppercase tracking-wide bg-violet-100 dark:bg-violet-400/10 text-violet-700 dark:text-violet-300 px-3 py-1.5 rounded-lg ring-1 ring-inset ring-violet-400/30 hover:bg-violet-200 dark:hover:bg-violet-400/20 transition">
              ver todas <ArrowRight size={12} />
            </Link>
          </div>
          {/* Título herói: JANELA 2K */}
          <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-neutral-500 dark:text-violet-200/60">Janela de verificação</p>
          <h2 className="font-display cyber-kpi text-6xl font-black leading-none mt-1 text-violet-600 dark:text-violet-200">2K<span className="text-3xl align-top">+</span></h2>
          <div className="mt-4 flex items-baseline gap-2">
            <span className="font-display text-3xl font-black tabular-nums text-neutral-900 dark:text-emerald-100">{b['2k'] || 0}</span>
            <span className="text-[11px] uppercase tracking-wide text-neutral-500 dark:text-emerald-200/60">verificadas</span>
          </div>
          <div className="mt-3 flex items-center justify-between rounded-lg border border-neutral-200 dark:border-emerald-400/15 bg-white/50 dark:bg-[#07160f]/50 px-3 py-2">
            <span className="text-[11px] uppercase tracking-wide text-neutral-500 dark:text-emerald-200/60">Total de BMs</span>
            <span className="font-display font-bold tabular-nums text-neutral-900 dark:text-emerald-100">{b.total || 0}</span>
          </div>
          {(b.unknown || 0) > 0 && (
            <div className="mt-2 rounded-lg border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-950/30 px-3 py-2">
              <p className="text-[11px] font-semibold text-amber-700 dark:text-amber-300"><b className="tabular-nums">{b.unknown}</b> aguardando análise</p>
            </div>
          )}
        </Card>

        <Card className="lg:col-span-2">
          <div className="flex items-center gap-2.5 mb-4">
            <span className="cyber-ibadge blue h-8 w-8"><BarChart3 size={16} /></span>
            <h2 className="font-display text-base font-extrabold uppercase tracking-wider text-neutral-900 dark:text-emerald-100">Distribuição dos Runs</h2>
          </div>
          <DistributionBar segments={segments} />
          {stats.failures && stats.failures.length > 0 && (
            <div className="mt-5 pt-5 border-t border-rose-200 dark:border-rose-500/30">
              <h3 className="text-xs font-bold uppercase tracking-wider text-rose-700 dark:text-rose-300 mb-4 flex items-center gap-2">
                <AlertTriangle size={15} />
                Análise de Falhas
                <span className="ml-auto bg-rose-100 dark:bg-rose-900/40 px-2.5 py-1 rounded text-[11px] font-bold tabular-nums">{r.failed || 0} total</span>
              </h3>
              <ul className="space-y-3">
                {stats.failures.map((f) => (
                  <li key={f.reason} className="text-xs">
                    <div className="flex items-center justify-between gap-3 mb-1.5">
                      <span className="truncate font-medium text-neutral-700 dark:text-emerald-100/80">{f.reason}</span>
                      <span className="shrink-0 tabular-nums bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 px-2 py-1 rounded font-bold">{f.count} ({f.pct}%)</span>
                    </div>
                    <div className="h-2 rounded-full bg-neutral-200 dark:bg-neutral-800 overflow-hidden">
                      <div className="h-full bg-gradient-to-r from-rose-400 to-rose-600 rounded-full" style={{ width: `${f.pct}%` }} />
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
      </div>
    </>
  )
}

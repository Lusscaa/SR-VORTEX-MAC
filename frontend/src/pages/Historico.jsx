import { useMemo, useState } from "react"
import { Search, Clock, User, KeyRound, FileText, Wifi, CheckCircle, Cpu, Building2, Download, Timer, Activity, AlertTriangle, Zap } from "lucide-react"
import { api } from "../api.js"
import { usePolling } from "../hooks.js"
import { timeAgo, toMs, fmtDuration } from "../utils.js"
import { Badge, Card, CopyButton, Freshness, PageHead, Skeleton, inputCls } from "../components/ui.jsx"

const TONE = {
  success: 'green', incomplete: 'amber', failed: 'danger',
  running: 'running', queued: 'neutral', cancelled: 'neutral', assumed: 'amber',
}
const LABEL = {
  success: '100%', incomplete: 'incompleto', failed: 'falhou',
  running: 'rodando', queued: 'na fila', cancelled: 'cancelada', assumed: 'assumido',
}

// Chips de filtro por status.
const FILTERS = [
  { key: 'all', label: 'Todos' },
  { key: 'success', label: '100%' },
  { key: 'incomplete', label: 'Incompletos' },
  { key: 'failed', label: 'Falhas' },
  { key: 'assumed', label: 'Assumidos' },
  { key: 'cancelled', label: 'Cancelados' },
]

// CNPJ 14 dígitos → 00.000.000/0000-00.
function formatCnpj(v) {
  const d = String(v || '').replace(/\D/g, '')
  if (d.length !== 14) return v || ''
  return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`
}

function fmtDT(t) {
  const ms = toMs(t)
  if (!ms) return { d: '—', h: '' }
  const dt = new Date(ms)
  return {
    d: dt.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit' }),
    h: dt.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }),
  }
}

function Th({ icon: Icon, children }) {
  return (
    <th className="px-4 py-3 text-left align-middle">
      <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-neutral-500 dark:text-emerald-300/70">
        {Icon && <Icon size={13} />} {children}
      </span>
    </th>
  )
}

// Valor mono + copiar (conta, CNPJ, senha, proxy, id do perfil…).
function Mono({ value, className = "", max = "max-w-[220px]", strong }) {
  if (!value) return <span className="text-neutral-300 dark:text-neutral-700">—</span>
  return (
    <span className={`inline-flex items-center gap-1.5 font-mono text-xs ${strong ? 'font-bold text-neutral-800 dark:text-emerald-50' : ''} ${className}`}>
      <span className={`truncate ${max}`} title={value}>{value}</span>
      <CopyButton text={value} label="" className="shrink-0 text-neutral-400 hover:text-neutral-700 dark:hover:text-emerald-200" />
    </span>
  )
}

// Mini-card de resumo (topo).
function SumCard({ tone, icon: Icon, label, value }) {
  const t = { blue: 'text-sky-600 dark:text-sky-300', green: 'text-emerald-600 dark:text-emerald-300', gold: 'text-amber-600 dark:text-amber-300', red: 'text-rose-600 dark:text-rose-300' }
  return (
    <Card className="py-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-neutral-500 dark:text-emerald-200/60">{label}</p>
          <p className={`font-display cyber-kpi text-2xl font-black tabular-nums mt-0.5 ${t[tone]}`}>{value}</p>
        </div>
        <span className={`cyber-ibadge ${tone === 'blue' ? 'blue' : tone === 'green' ? 'green' : tone === 'gold' ? 'gold' : 'red'} h-9 w-9 shrink-0`}><Icon size={16} /></span>
      </div>
    </Card>
  )
}

export default function Historico() {
  const { data: runsData, error, lastOk } = usePolling(() => api.runs(), 5000)
  const runs = useMemo(() => runsData || [], [runsData])
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  // Seleção por checkbox (run_id). Vazio = exporta todas as visíveis.
  const [selected, setSelected] = useState(() => new Set())
  const toggleOne = (id) => setSelected((prev) => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n
  })

  // Contagem por status (para os chips).
  const counts = useMemo(() => {
    const c = {}
    for (const r of runs) c[r.status] = (c[r.status] || 0) + 1
    return c
  }, [runs])

  const shown = useMemo(() => {
    let list = runs
    if (filter !== 'all') list = list.filter((r) => r.status === filter)
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter((r) =>
        (r.cnpj || '').toLowerCase().includes(q) ||
        (r.label || '').toLowerCase().includes(q) ||
        (r.profile_serial_number || r.profile_user_id || '').toLowerCase().includes(q) ||
        (r.proxy_host || '').toLowerCase().includes(q)
      )
    }
    return list
  }, [runs, query, filter])

  // Selecionar todos / origem do export / contagem de contas únicas.
  const allSel = shown.length > 0 && shown.every((r) => selected.has(r.run_id))
  const toggleAll = () => setSelected((prev) => {
    const n = new Set(prev)
    if (allSel) { for (const r of shown) n.delete(r.run_id) }
    else { for (const r of shown) n.add(r.run_id) }
    return n
  })
  const exportSrc = useMemo(
    () => (selected.size ? shown.filter((r) => selected.has(r.run_id)) : shown),
    [shown, selected],
  )
  const exportCount = useMemo(
    () => new Set(exportSrc.map((r) => (r.label || '').trim()).filter(Boolean)).size,
    [exportSrc],
  )

  // Exporta um BLOCO por CONTA (dedupe por login) — respeita filtro + busca.
  // Formato pedido pelo operador:
  //   CONTA N
  //   Data da verificação: <data>          (só se a conta tiver run 100%)
  //   LOGIN | SENHA | 2FA | EMAIL | EMAILSENHA | PROXY   (só os que existem)
  const exportContas = () => {
    const seen = new Map(); const order = []
    for (const r of exportSrc) {
      const login = (r.label || '').trim()
      if (!login) continue
      let a = seen.get(login)
      if (!a) {
        a = { login, senha: '', f2a: '', email: '', emailPwd: '', proxy: '', verif: '' }
        seen.set(login, a); order.push(login)
      }
      // Preenche o que ainda falta (runs vêm da mais nova pra mais antiga).
      if (!a.senha && r.account_password) a.senha = r.account_password
      if (!a.f2a && r.account_totp_seed) a.f2a = r.account_totp_seed
      if (!a.email && r.account_email_address) a.email = r.account_email_address
      if (!a.emailPwd && r.account_email_password) a.emailPwd = r.account_email_password
      if (!a.proxy && r.proxy_host) a.proxy = r.proxy_host
      if (!a.verif && r.status === 'success') {
        const dt = fmtDT(r.finished_at || r.created_at)
        a.verif = `${dt.d}${dt.h ? ` ${dt.h}` : ''}`.trim()
      }
    }
    const blocks = order.map((login, i) => {
      const a = seen.get(login)
      const out = [`CONTA ${i + 1}`]
      if (a.verif) out.push(`Data da verificação: ${a.verif}`)
      out.push([a.login, a.senha, a.f2a, a.email, a.emailPwd, a.proxy].filter(Boolean).join(' | '))
      return out.join('\n')
    })
    const text = blocks.join('\n\n') + '\n'
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `contas_${new Date().toISOString().slice(0, 10)}.txt`
    a.click()
  }

  if (!runsData && !error) {
    return (
      <>
        <PageHead title="Histórico" subtitle="Data, credenciais, perfil AdsPower e proxy usados em cada run" />
        <Skeleton className="h-[60vh]" />
      </>
    )
  }

  const s = { total: runs.length, success: counts.success || 0, incomplete: counts.incomplete || 0, failed: counts.failed || 0 }

  return (
    <>
      <PageHead title="Histórico" subtitle="Data, credenciais, perfil AdsPower e proxy usados em cada run">
        <Freshness lastOk={lastOk} error={error} />
      </PageHead>

      {/* Resumo */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
        <SumCard tone="blue"  icon={Activity}      label="Total de runs" value={s.total} />
        <SumCard tone="green" icon={CheckCircle}   label="100% concluídos" value={s.success} />
        <SumCard tone="gold"  icon={Zap}           label="Incompletos" value={s.incomplete} />
        <SumCard tone="red"   icon={AlertTriangle} label="Falhas" value={s.failed} />
      </div>

      {/* Busca + filtros + exportar */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[240px] max-w-xl">
          <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-400 pointer-events-none" />
          <input
            value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="Buscar por conta, CNPJ, perfil ou proxy…"
            className={`${inputCls} pl-10 w-full`}
          />
        </div>
        {selected.size > 0 && (
          <button
            type="button" onClick={() => setSelected(new Set())}
            className="rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-2 text-xs font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10"
          >
            Limpar seleção
          </button>
        )}
        <button
          type="button" onClick={exportContas} disabled={exportCount === 0}
          title="Exporta um bloco por conta (login, senha, 2FA, email, senha do email e proxy) — só os dados que existem. Marque linhas para exportar apenas elas."
          className="inline-flex items-center gap-2 rounded-lg cyber-btn px-4 py-2 text-sm disabled:cursor-not-allowed"
        >
          <Download size={15} /> Exportar {selected.size ? `${exportCount} selecionada${exportCount === 1 ? '' : 's'}` : 'Contas'}
        </button>
      </div>

      <div className="mb-4 flex flex-wrap gap-1.5">
        {FILTERS.map((f) => {
          const n = f.key === 'all' ? runs.length : (counts[f.key] || 0)
          const active = filter === f.key
          return (
            <button
              key={f.key} type="button" onClick={() => setFilter(f.key)}
              className={`rounded-full px-3 py-1 text-xs font-bold uppercase tracking-wide transition ${
                active
                  ? 'bg-neutral-900 text-white dark:bg-gradient-to-r dark:from-amber-300/90 dark:to-emerald-400/90 dark:text-[#06130d] dark:shadow-[0_0_16px_-4px_rgba(52,211,153,0.6)]'
                  : 'border border-neutral-300 dark:border-emerald-400/25 text-neutral-500 dark:text-emerald-100/70 hover:bg-neutral-100 dark:hover:bg-emerald-400/10'
              }`}
            >
              {f.label} {n > 0 && <span className="tabular-nums opacity-70">{n}</span>}
            </button>
          )
        })}
        {(query || filter !== 'all') && (
          <span className="self-center text-xs text-neutral-500 dark:text-emerald-200/60 ml-1">
            Mostrando <b className="tabular-nums">{shown.length}</b> de <b className="tabular-nums">{runs.length}</b>
          </span>
        )}
      </div>

      <Card className="p-0 overflow-hidden">
        {shown.length === 0 ? (
          <p className="text-sm text-neutral-400 py-14 text-center">
            {runs.length === 0 ? 'Nenhuma run ainda.' : 'Nenhuma run encontrada com esse filtro.'}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-neutral-200 dark:border-emerald-400/25 bg-neutral-50 dark:bg-[#07160f]/80">
                  <th className="px-3 py-3 w-10 text-center align-middle">
                    <input
                      type="checkbox" checked={allSel} onChange={toggleAll}
                      title="Selecionar todas as visíveis"
                      className="h-4 w-4 cursor-pointer accent-emerald-500 align-middle"
                    />
                  </th>
                  <Th icon={Clock}>Data / Hora</Th>
                  <Th icon={User}>Login</Th>
                  <Th icon={KeyRound}>Senha</Th>
                  <Th icon={FileText}>CNPJ</Th>
                  <Th icon={Cpu}>AdsPower</Th>
                  <Th icon={Wifi}>Conexão</Th>
                  <Th icon={Timer}>Duração</Th>
                  <Th icon={CheckCircle}>Status</Th>
                  <Th icon={Building2}>BM ID</Th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => {
                  const dt = fmtDT(r.created_at)
                  const dur = r.started_at ? fmtDuration(r.started_at, r.finished_at) : null
                  return (
                    <tr key={r.run_id} className={`border-b border-neutral-100 dark:border-emerald-400/10 transition-colors ${
                      selected.has(r.run_id)
                        ? 'bg-emerald-50 dark:bg-emerald-400/[0.09]'
                        : 'dark:even:bg-emerald-400/[0.015] hover:bg-lime-50/50 dark:hover:bg-emerald-400/[0.06]'
                    }`}>
                      <td className="px-3 py-3 text-center align-middle">
                        <input
                          type="checkbox" checked={selected.has(r.run_id)} onChange={() => toggleOne(r.run_id)}
                          className="h-4 w-4 cursor-pointer accent-emerald-500 align-middle"
                        />
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <div className="font-display font-bold text-neutral-800 dark:text-emerald-50 tabular-nums leading-tight">{dt.d}</div>
                        <div className="text-xs text-neutral-500 dark:text-emerald-200/50 tabular-nums">{dt.h} <span className="opacity-70">· {timeAgo(r.created_at)}</span></div>
                      </td>
                      <td className="px-4 py-3"><Mono value={r.label} max="max-w-[190px]" strong /></td>
                      <td className="px-4 py-3"><Mono value={r.account_password} max="max-w-[130px]" /></td>
                      <td className="px-4 py-3"><Mono value={formatCnpj(r.cnpj)} max="max-w-[170px]" strong /></td>
                      <td className="px-4 py-3">
                        {(r.profile_serial_number || r.profile_user_id)
                          ? <span className="inline-block bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-300 ring-1 ring-inset ring-blue-400/30 px-2.5 py-1 rounded-md font-bold text-xs">{r.profile_serial_number || r.profile_user_id}</span>
                          : <span className="text-neutral-300 dark:text-neutral-700">—</span>}
                      </td>
                      <td className="px-4 py-3">
                        {r.proxy_host
                          ? <Mono value={r.proxy_host} max="max-w-[160px]" />
                          : <span className="inline-flex items-center gap-1 rounded-md bg-sky-100 dark:bg-sky-500/15 text-sky-700 dark:text-sky-300 ring-1 ring-inset ring-sky-400/30 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide">5G / VPN</span>}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        {dur ? <span className="font-mono text-xs font-semibold text-neutral-700 dark:text-emerald-100/80 tabular-nums">{dur}</span> : <span className="text-neutral-300 dark:text-neutral-700">—</span>}
                      </td>
                      <td className="px-4 py-3"><Badge tone={TONE[r.status] || 'neutral'}>{LABEL[r.status] || r.status}</Badge></td>
                      <td className="px-4 py-3"><Mono value={r.business_id} max="max-w-[150px]" /></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  )
}

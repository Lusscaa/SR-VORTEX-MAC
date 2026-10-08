import { useState, useEffect, useMemo } from 'react'
import {
  AlertCircle, Trash2, RefreshCw, Copy, Download, Recycle,
  User, KeyRound, Shield, Mail, Lock, Search,
} from 'lucide-react'
import { api } from '../api.js'
import { Badge, Card, CopyButton, PageHead, Skeleton, inputCls } from '../components/ui.jsx'

// Classifica o motivo do descarte a partir do remark → rótulo + cor + ícone.
function classifyReason(remark) {
  const t = (remark || '').toLowerCase()
  if (t.includes('account_disabled') || t.includes('desativ') || t.includes('irrecuper'))
    return { key: 'disabled', label: 'Conta desativada', tone: 'neutral' }
  if (t.includes('soft-block') || t.includes('soft block') || t.includes('bounced'))
    return { key: 'softblock', label: 'Soft-block / IP', tone: 'amber' }
  if (t.includes('senha') || t.includes('password') || t.includes('incorreta'))
    return { key: 'senha', label: 'Senha incorreta', tone: 'danger' }
  if (t.includes('timeout') || t.includes('locator') || t.includes('handler') || t.includes('selector'))
    return { key: 'timeout', label: 'Timeout / erro', tone: 'warn' }
  return { key: 'other', label: 'Erro terminal', tone: 'neutral' }
}
const cleanRemark = (r) => (r ? r.replace('[PENDING_DISCARD] ', '') : 'Erro terminal')

const REASON_FILTERS = [
  { key: 'all', label: 'Todos' },
  { key: 'softblock', label: 'Soft-block' },
  { key: 'senha', label: 'Senha' },
  { key: 'disabled', label: 'Desativadas' },
  { key: 'timeout', label: 'Timeout' },
  { key: 'other', label: 'Outros' },
]

// Campo de credencial rotulado + copiar (só aparece se tiver valor).
function CredField({ icon: Icon, label, value }) {
  if (!value) return null
  return (
    <div className="rounded-lg border border-neutral-200 dark:border-emerald-400/20 bg-white/60 dark:bg-[#07160f]/50 px-2.5 py-1.5 min-w-0">
      <p className="flex items-center gap-1 text-[9px] font-bold uppercase tracking-[0.14em] text-neutral-400 dark:text-emerald-300/50">
        {Icon && <Icon size={10} />} {label}
      </p>
      <div className="flex items-center gap-1.5 mt-0.5">
        <span className="font-mono text-xs font-semibold truncate text-neutral-800 dark:text-emerald-50" title={value}>{value}</span>
        <CopyButton text={value} label="" className="shrink-0 text-neutral-400 hover:text-emerald-500 dark:hover:text-emerald-300" />
      </div>
    </div>
  )
}

export default function PendingDiscard() {
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [msg, setMsg] = useState(null) // { ok, text }

  useEffect(() => { loadAccounts() }, [])

  const loadAccounts = async () => {
    setLoading(true)
    try {
      const data = await api.getPendingDiscardAccounts()
      setAccounts(data || [])
    } catch (e) {
      setMsg({ ok: false, text: `Erro ao carregar: ${e.message}` })
    } finally {
      setLoading(false)
    }
  }

  // Anexa a classificação em cada conta (uma vez).
  const classified = useMemo(
    () => accounts.map((a) => ({ ...a, _r: classifyReason(a.remark) })),
    [accounts],
  )
  const counts = useMemo(() => {
    const c = {}
    for (const a of classified) c[a._r.key] = (c[a._r.key] || 0) + 1
    return c
  }, [classified])

  const shown = useMemo(() => {
    let list = classified
    if (filter !== 'all') list = list.filter((a) => a._r.key === filter)
    const q = query.trim().toLowerCase()
    if (q) list = list.filter((a) =>
      (a.login || '').toLowerCase().includes(q) ||
      (a.email_address || '').toLowerCase().includes(q) ||
      (a.remark || '').toLowerCase().includes(q))
    return list
  }, [classified, filter, query])

  const toggleSelect = (email) => setSelectedIds((prev) => {
    const n = new Set(prev); n.has(email) ? n.delete(email) : n.add(email); return n
  })
  const allShownSelected = shown.length > 0 && shown.every((a) => selectedIds.has(a.email))
  const toggleAll = () => setSelectedIds((prev) => {
    const n = new Set(prev)
    if (allShownSelected) { for (const a of shown) n.delete(a.email) }
    else { for (const a of shown) n.add(a.email) }
    return n
  })

  const selectedAccounts = () => classified.filter((a) => selectedIds.has(a.email))

  const copySelected = async () => {
    const sel = selectedAccounts()
    if (!sel.length) return
    const lines = sel.map((a) => [a.login, a.password, a.totp_seed, a.email_address, a.email_pass].filter(Boolean).join(':')).join('\n')
    try {
      await navigator.clipboard.writeText(lines)
      setMsg({ ok: true, text: `${sel.length} credencial(is) copiada(s).` })
    } catch (e) { setMsg({ ok: false, text: `Erro ao copiar: ${e.message}` }) }
  }

  // Exporta selecionadas (ou todas visíveis) no formato bloco-por-conta.
  const exportSel = () => {
    const src = selectedIds.size ? selectedAccounts() : shown
    if (!src.length) return
    const blocks = src.map((a, i) => {
      const vals = [a.login, a.password, a.totp_seed, a.email_address, a.email_pass].filter(Boolean).join(' | ')
      return `CONTA ${i + 1}\nMotivo: ${cleanRemark(a.remark)}\n${vals}`
    })
    const blob = new Blob([blocks.join('\n\n') + '\n'], { type: 'text/plain;charset=utf-8' })
    const el = document.createElement('a')
    el.href = URL.createObjectURL(blob)
    el.download = `descartes_${new Date().toISOString().slice(0, 10)}.txt`
    el.click()
  }

  const discardSelected = async () => {
    if (!selectedIds.size) return
    if (!window.confirm(`Descartar ${selectedIds.size} conta(s)? Saem do pool e não voltam. Não dá pra desfazer.`)) return
    setBusy(true); setMsg(null)
    try {
      const data = await api.discardAccountsManual(Array.from(selectedIds))
      const n = (data.discarded || []).length
      if (n > 0) {
        setAccounts((prev) => prev.filter((a) => !selectedIds.has(a.email)))
        setSelectedIds(new Set())
        setMsg({ ok: true, text: `✓ ${n} conta(s) descartada(s).` })
      } else {
        setMsg({ ok: false, text: 'Nenhuma conta foi descartada.' })
      }
    } catch (e) {
      setMsg({ ok: false, text: `Erro ao descartar: ${e.message}` })
    } finally { setBusy(false) }
  }

  // Resgata: devolve a(s) conta(s) ao pool (quando o erro foi de IP, não da conta).
  const rescueSelected = async () => {
    if (!selectedIds.size) return
    if (!window.confirm(`Resgatar ${selectedIds.size} conta(s) para o pool?\n\nElas voltam a ficar disponíveis para novos runs. Use quando o erro foi de IP/soft-block — não da conta.`)) return
    setBusy(true); setMsg(null)
    const emails = Array.from(selectedIds)
    let ok = 0
    for (const email of emails) {
      try { await api.rescueAccount(email); ok++ } catch { /* segue */ }
    }
    if (ok > 0) {
      setAccounts((prev) => prev.filter((a) => !selectedIds.has(a.email)))
      setSelectedIds(new Set())
    }
    setMsg({ ok: ok > 0, text: ok > 0 ? `✓ ${ok} conta(s) resgatada(s) ao pool.` : 'Nenhuma conta resgatada.' })
    setBusy(false)
    loadAccounts()
  }

  return (
    <>
      <PageHead title="Descartes" subtitle="Contas que falharam com erro terminal — revise o motivo antes de descartar">
        {!loading && (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-rose-100 dark:bg-rose-500/15 text-rose-700 dark:text-rose-300 ring-1 ring-inset ring-rose-400/30 px-3 py-1 text-xs font-bold uppercase tracking-wide">
            <AlertCircle size={13} /> {accounts.length} pendente{accounts.length === 1 ? '' : 's'}
          </span>
        )}
      </PageHead>

      <div className="mb-4 rounded-lg border border-rose-300/60 dark:border-rose-500/30 bg-rose-50 dark:bg-rose-950/25 px-4 py-3">
        <p className="text-sm text-rose-800 dark:text-rose-200">
          Contas paradas por erro terminal (senha errada, soft-block, conta desativada…). Você pode <b>copiar/exportar</b> as credenciais, <b>resgatar</b> ao pool (se o erro foi de IP) ou <b>descartar</b> de vez.
        </p>
      </div>

      {msg && (
        <p className={`mb-4 rounded-lg border px-3 py-2 text-sm ${msg.ok ? 'border-emerald-300 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300' : 'border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/30 text-rose-700 dark:text-rose-300'}`}>{msg.text}</p>
      )}

      {loading ? (
        <Skeleton className="h-[50vh]" />
      ) : accounts.length === 0 ? (
        <Card className="py-16 text-center">
          <p className="text-neutral-500 dark:text-emerald-200/70 font-medium">Nenhuma conta pendente de descarte 🎉</p>
        </Card>
      ) : (
        <>
          {/* Busca + filtros por motivo */}
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <div className="relative flex-1 min-w-[240px] max-w-xl">
              <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-400 pointer-events-none" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Buscar por login, email ou motivo…" className={`${inputCls} pl-10 w-full`} />
            </div>
          </div>
          <div className="mb-4 flex flex-wrap gap-1.5">
            {REASON_FILTERS.map((f) => {
              const n = f.key === 'all' ? accounts.length : (counts[f.key] || 0)
              const active = filter === f.key
              return (
                <button key={f.key} type="button" onClick={() => setFilter(f.key)}
                  className={`rounded-full px-3 py-1 text-xs font-bold uppercase tracking-wide transition ${
                    active
                      ? 'bg-neutral-900 text-white dark:bg-gradient-to-r dark:from-amber-300/90 dark:to-emerald-400/90 dark:text-[#06130d] dark:shadow-[0_0_16px_-4px_rgba(52,211,153,0.6)]'
                      : 'border border-neutral-300 dark:border-emerald-400/25 text-neutral-500 dark:text-emerald-100/70 hover:bg-neutral-100 dark:hover:bg-emerald-400/10'
                  }`}>
                  {f.label} {n > 0 && <span className="tabular-nums opacity-70">{n}</span>}
                </button>
              )
            })}
          </div>

          {/* Barra de ações */}
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <button type="button" onClick={toggleAll} className="rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-2 text-xs font-bold uppercase tracking-wide hover:bg-neutral-100 dark:hover:bg-emerald-400/10">
              {allShownSelected ? 'Desmarcar' : 'Selecionar todas'}
            </button>
            {selectedIds.size > 0 && (
              <>
                <span className="text-xs font-bold text-neutral-500 dark:text-emerald-200/70 tabular-nums">{selectedIds.size} selecionada(s)</span>
                <button type="button" onClick={copySelected} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg border border-sky-300 dark:border-sky-400/40 text-sky-700 dark:text-sky-300 px-3 py-2 text-xs font-bold uppercase tracking-wide hover:bg-sky-50 dark:hover:bg-sky-400/10 disabled:opacity-40"><Copy size={13} /> Copiar</button>
                <button type="button" onClick={exportSel} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg cyber-btn px-3 py-2 text-xs disabled:opacity-40"><Download size={13} /> Exportar</button>
                <button type="button" onClick={rescueSelected} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-300 dark:border-emerald-400/40 text-emerald-700 dark:text-emerald-300 px-3 py-2 text-xs font-bold uppercase tracking-wide hover:bg-emerald-50 dark:hover:bg-emerald-400/10 disabled:opacity-40"><Recycle size={13} /> Resgatar</button>
                <button type="button" onClick={discardSelected} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg border border-rose-300 dark:border-rose-400/40 text-rose-700 dark:text-rose-300 px-3 py-2 text-xs font-bold uppercase tracking-wide hover:bg-rose-50 dark:hover:bg-rose-400/10 disabled:opacity-40"><Trash2 size={13} /> Descartar</button>
              </>
            )}
            <button type="button" onClick={loadAccounts} disabled={busy || loading} title="Recarregar" className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-500 dark:text-emerald-200/70 px-3 py-2 text-xs font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10">
              <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Atualizar
            </button>
          </div>

          {/* Cards de conta */}
          {shown.length === 0 ? (
            <Card className="py-12 text-center"><p className="text-sm text-neutral-400">Nenhuma conta com esse filtro.</p></Card>
          ) : (
            <div className="space-y-2.5">
              {shown.map((a) => {
                const sel = selectedIds.has(a.email)
                return (
                  <Card key={a.email} className={`transition ${sel ? 'ring-2 ring-emerald-400/60 dark:ring-emerald-400/70' : ''}`}>
                    <div className="flex items-start gap-3">
                      <input type="checkbox" checked={sel} onChange={() => toggleSelect(a.email)} className="mt-1 h-4 w-4 shrink-0 cursor-pointer accent-emerald-500" />
                      <div className="flex-1 min-w-0">
                        <div className="flex flex-wrap items-center gap-2 mb-2.5">
                          <span className="font-display font-bold text-sm text-neutral-900 dark:text-emerald-50 truncate">{a.login || a.email}</span>
                          <Badge tone={a._r.tone}>{a._r.label}</Badge>
                        </div>
                        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
                          <CredField icon={User} label="Login" value={a.login} />
                          <CredField icon={KeyRound} label="Senha" value={a.password} />
                          <CredField icon={Shield} label="2FA" value={a.totp_seed} />
                          <CredField icon={Mail} label="Email" value={a.email_address} />
                          <CredField icon={Lock} label="Senha email" value={a.email_pass} />
                        </div>
                        <p className="mt-2 text-xs text-neutral-500 dark:text-emerald-200/50">{cleanRemark(a.remark)}</p>
                      </div>
                    </div>
                  </Card>
                )
              })}
            </div>
          )}
        </>
      )}
    </>
  )
}

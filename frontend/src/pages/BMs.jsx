import { useEffect, useState } from "react"
import { CheckCircle, XCircle, Clock, BarChart3, RefreshCw } from "lucide-react"
import { api } from "../api.js"
import { Badge, Card, CopyButton, PageHead, Stat, btnPrimary } from "../components/ui.jsx"

function verifBadge(tier, statusDetail) {
  // Usa o status_detail cru da Graph (verified/pending/rejected/…) p/ separar
  // "em análise" de "rejeitada" — o tier sozinho ('2k'/'not_2k') juntava
  // pendente + rejeitada num só balde. Default (recém-submetida, sem check
  // ainda, ou pendente) = "Em análise" — a Meta leva ~2 dias úteis.
  const s = (statusDetail || '').toLowerCase()
  if (s === 'verified' || tier === '2k') return <Badge tone="green">Verificada</Badge>
  if (['rejected', 'failed', 'expired', 'revoked'].includes(s))
    return <Badge tone="red">Rejeitada</Badge>
  if (s.includes('token_expirado') || s.includes('token_invalido'))
    return <Badge tone="neutral">Token expirado</Badge>
  return <Badge tone="warn">Em análise</Badge>
}

// Status MANUAL (rótulo do operador) — cores próprias, sobrepõe o automático.
// key '' = automático (usa o sinal da Graph).
const MANUAL = {
  analise:   { label: 'Em análise',     cls: 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 dark:ring-amber-400/30' },
  aprovada1: { label: '1ª BM aprovada', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 dark:ring-sky-400/30' },
  aprovada2: { label: '2ª BM aprovada', cls: 'bg-teal-100 text-teal-700 dark:bg-teal-500/15 dark:text-teal-300 dark:ring-teal-400/30' },
  '2k':      { label: '2K ON',          cls: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-200 dark:ring-emerald-400/50 font-black' },
  banida:    { label: 'Banida',         cls: 'bg-rose-100 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300 dark:ring-rose-400/40' },
}
const MANUAL_OPTIONS = [
  { key: '', label: '— Automático (Meta) —' },
  { key: 'analise', label: 'Em análise' },
  { key: 'aprovada1', label: '1ª BM aprovada' },
  { key: 'aprovada2', label: '2ª BM aprovada' },
  { key: '2k', label: '2K ON' },
  { key: 'banida', label: 'Banida' },
]
// Cor das letras por status (deixa o dropdown vivo em vez de branco morto).
const MANUAL_COLOR = {
  '': '#6ee7b7',        // automático → verde suave
  analise: '#fbbf24',   // âmbar
  aprovada1: '#38bdf8', // azul
  aprovada2: '#2dd4bf', // teal
  '2k': '#34d399',      // verde forte
  banida: '#fb7185',    // vermelho
}
function manualBadge(key) {
  const m = MANUAL[key]
  if (!m) return null
  return <span className={`inline-flex items-center whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-bold uppercase tracking-wide ring-1 ring-inset ${m.cls}`}>{m.label}</span>
}

// Linha de credencial com botão copiar — login/senha/2FA da conta da BM.
function CredRow({ label, value }) {
  return (
    <div className="flex items-center justify-between gap-2 text-xs">
      <span className="text-neutral-400 shrink-0">{label}</span>
      <div className="flex items-center gap-1.5 min-w-0">
        <span className="font-mono text-right break-all">{value || '—'}</span>
        {value ? (
          <CopyButton text={value} label="Copiar" className="shrink-0 text-[10px] px-1.5 py-0.5" />
        ) : null}
      </div>
    </div>
  )
}

function BMCard({ bm, onStatus, saving }) {
  const rows = [
    ['CNPJ', bm.cnpj],
    ['Conta de anúncios', bm.ad_account_id ? `act_${bm.ad_account_id}` : null],
    ['Status Meta', bm.status_detail],
  ]

  const manual = bm.manual_status || ''

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <p className="font-bold text-base leading-snug text-neutral-900 dark:text-emerald-50">{bm.portfolio_name || '—'}</p>
          <p className="font-mono text-xs text-neutral-500 dark:text-emerald-200/50 mt-1">BM ID: {bm.business_id}</p>
        </div>
        <div className="shrink-0">
          {/* Badge efetivo: manual sobrepõe o automático da Meta. */}
          {manual ? manualBadge(manual) : verifBadge(bm.tier, bm.status_detail)}
        </div>
      </div>

      <div className="grid gap-2 border-t-2 border-neutral-200 dark:border-neutral-700 pt-3">
        {rows.map(([k, v]) => (
          <div key={k} className="flex items-center justify-between gap-2 text-xs">
            <span className="font-medium text-neutral-600 dark:text-neutral-400">{k}</span>
            <span className="font-mono text-right break-all text-neutral-900 dark:text-neutral-100">{v || '—'}</span>
          </div>
        ))}
      </div>

      <div className="grid gap-2.5 border-t-2 border-neutral-200 dark:border-emerald-400/15 pt-3">
        <span className="text-xs uppercase tracking-wider font-bold text-neutral-700 dark:text-emerald-200">🔐 Credenciais AdsPower</span>
        <CredRow label="Login" value={bm.account_email} />
        <CredRow label="Senha" value={bm.account_password} />
        <CredRow label="2FA" value={bm.account_totp_seed} />
      </div>

      {/* Status MANUAL — operador escolhe o rótulo (sobrepõe o automático). */}
      <div className="grid gap-1.5 border-t-2 border-neutral-200 dark:border-emerald-400/15 pt-3">
        <span className="text-xs uppercase tracking-wider font-bold text-neutral-700 dark:text-emerald-200">🏷️ Status manual</span>
        <select
          value={manual} disabled={saving}
          onChange={(e) => onStatus(bm.business_id, e.target.value)}
          style={{ color: MANUAL_COLOR[manual] || undefined }}
          className="w-full rounded-lg border border-neutral-300 dark:border-emerald-400/30 bg-transparent dark:bg-[#07130d]/60 px-3 py-2 text-sm font-bold outline-none focus:border-neutral-500 dark:focus:border-amber-300/70 disabled:opacity-50"
        >
          {MANUAL_OPTIONS.map((o) => (
            <option key={o.key} value={o.key} style={{ color: MANUAL_COLOR[o.key], fontWeight: 700 }} className="bg-white dark:bg-[#07160f]">{o.label}</option>
          ))}
        </select>
      </div>
    </Card>
  )
}

function formatTimeAgo(ts) {
  if (!ts) return null
  const diff = Math.floor((Date.now() / 1000) - ts)
  if (diff < 60) return 'agora'
  if (diff < 3600) return `${Math.floor(diff / 60)}min atrás`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h atrás`
  return `${Math.floor(diff / 86400)}d atrás`
}

export default function BMs() {
  const [bms, setBms] = useState([])
  const [counts, setCounts] = useState({})
  const [busy, setBusy] = useState(false)
  const [savingId, setSavingId] = useState(null)
  const [msg, setMsg] = useState(null)
  const [refreshInfo, setRefreshInfo] = useState(null)

  const load = () => {
    api.bms().then(setBms).catch(() => {})
    api.stats().then((s) => setCounts(s.bms)).catch(() => {})
    api.refreshBmsStatus().then(setRefreshInfo).catch(() => {})
  }
  useEffect(load, [])

  // Status manual: salva no backend e atualiza o card localmente (sem recarregar).
  const changeStatus = async (businessId, status) => {
    setSavingId(businessId); setMsg(null)
    try {
      await api.setBmStatus(businessId, status)
      setBms((prev) => prev.map((b) => b.business_id === businessId ? { ...b, manual_status: status || null } : b))
    } catch (e) {
      setMsg(`Erro ao salvar status: ${e.message}`)
    } finally { setSavingId(null) }
  }

  const refresh = async () => {
    setBusy(true); setMsg(null)
    try {
      const s = await api.refreshBms()
      setMsg(`Verificação: ${s.atualizadas} atualizada(s), ${s.falhas} falha(s), ${s.sem_token} sem token.`)
      load()
    } catch (e) { setMsg(`Erro: ${e.message}`) }
    finally { setBusy(false) }
  }

  return (
    <>
      <PageHead title="Business Managers" subtitle="Gerenciar e monitorar status de verificação">
        <div className="flex items-center gap-3">
          {refreshInfo?.last_refresh && (
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              Auto-refresh {refreshInfo.interval_hours}h · último {formatTimeAgo(refreshInfo.last_refresh)}
            </span>
          )}
          <button onClick={refresh} disabled={busy} className={`${btnPrimary} px-4 py-2.5 text-sm font-semibold flex items-center gap-2`}>
            <RefreshCw size={16} className={busy ? 'animate-spin' : ''} />
            {busy ? 'Atualizando…' : 'Atualizar verificação'}
          </button>
        </div>
      </PageHead>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <Card className="border border-blue-200 dark:border-blue-900 bg-gradient-to-br from-blue-50 to-cyan-50 dark:from-blue-950/30 dark:to-cyan-950/30">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="text-xs font-semibold uppercase text-blue-600 dark:text-blue-400 mb-1">Total de BMs</p>
              <p className="text-3xl font-bold text-blue-900 dark:text-blue-100">{counts.total || 0}</p>
            </div>
            <BarChart3 size={24} className="text-blue-500 opacity-70" />
          </div>
        </Card>

        <Card className="border border-green-200 dark:border-green-900 bg-gradient-to-br from-green-50 to-emerald-50 dark:from-green-950/30 dark:to-emerald-950/30">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="text-xs font-semibold uppercase text-green-600 dark:text-green-400 mb-1">Verificadas (2k+)</p>
              <p className="text-3xl font-bold text-green-900 dark:text-green-100">{counts['2k'] || 0}</p>
            </div>
            <CheckCircle size={24} className="text-green-500 opacity-70" />
          </div>
        </Card>

        <Card className="border border-red-200 dark:border-red-900 bg-gradient-to-br from-red-50 to-rose-50 dark:from-red-950/30 dark:to-rose-950/30">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="text-xs font-semibold uppercase text-red-600 dark:text-red-400 mb-1">Rejeitadas</p>
              <p className="text-3xl font-bold text-red-900 dark:text-red-100">{counts.not_2k || 0}</p>
            </div>
            <XCircle size={24} className="text-red-500 opacity-70" />
          </div>
        </Card>

        <Card className="border border-amber-200 dark:border-amber-900 bg-gradient-to-br from-amber-50 to-orange-50 dark:from-amber-950/30 dark:to-orange-950/30">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="text-xs font-semibold uppercase text-amber-600 dark:text-amber-400 mb-1">Em análise</p>
              <p className="text-3xl font-bold text-amber-900 dark:text-amber-100">{counts.unknown || 0}</p>
            </div>
            <Clock size={24} className="text-amber-500 opacity-70" />
          </div>
        </Card>
      </div>

      {msg && (
        <div className="mb-6 p-4 rounded-lg border-l-4 border-lime-500 bg-lime-50 dark:bg-lime-950/30 text-lime-700 dark:text-lime-300">
          <p className="text-sm font-medium">✓ {msg}</p>
        </div>
      )}

      {bms.length === 0 ? (
        <Card className="border border-neutral-300 dark:border-neutral-600">
          <p className="text-sm text-neutral-500 dark:text-neutral-400 py-8 text-center">📭 Nenhuma BM provisionada ainda.</p>
        </Card>
      ) : (
        <div>
          <h2 className="text-lg font-bold text-neutral-900 dark:text-neutral-100 mb-4 flex items-center gap-2">
            <BarChart3 size={20} className="text-purple-600" />
            {bms.length} Business Manager{bms.length !== 1 ? 's' : ''}
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
            {bms.map((bm) => <BMCard key={bm.business_id} bm={bm} onStatus={changeStatus} saving={savingId === bm.business_id} />)}
          </div>
        </div>
      )}
    </>
  )
}

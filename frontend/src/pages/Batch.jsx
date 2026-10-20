import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Database, Users, Wifi, Rocket, RefreshCw, Gauge, Building2, LogIn, Settings2,
  Search, ListPlus, Copy, Smartphone, Wallet, Target,
} from 'lucide-react'
import { api } from '../api.js'
import { Card, Stat, btnPrimary, btnGhost, inputCls } from '../components/ui.jsx'
import { CnpjCard } from '../components/CnpjCard.jsx'

// Gerador de lista de CNPJs (Casa dos Dados) — filtros → busca → adiciona ao pool.
function CnpjGenerator({ onAdded }) {
  const [f, setF] = useState({
    nome: '', somente_mei: false, somente_matriz: false, situacao_ativa: true,
    capital_min: '', capital_max: '', abertura_de: '', abertura_ate: '', uf: '', quantidade: 20,
  })
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }))
  const [results, setResults] = useState([])
  const [busy, setBusy] = useState(null) // 'search' | 'add' | null
  const [msg, setMsg] = useState(null)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [poolFiltrados, setPoolFiltrados] = useState(0)
  const qtd = Number(f.quantidade) || 20
  const totalPages = Math.max(1, Math.ceil(total / qtd))
  const lastTotalRef = useRef(0)

  const buscar = async (p = 1, aleatorio = false) => {
    setBusy('search'); setMsg(null); setPoolFiltrados(0)
    let targetPage = p
    if (aleatorio && lastTotalRef.current > qtd) {
      const maxPage = Math.max(1, Math.ceil(lastTotalRef.current / qtd))
      targetPage = Math.floor(Math.random() * maxPage) + 1
    }
    try {
      const payload = {
        ...f,
        quantidade: qtd, pagina: targetPage,
        capital_min: f.capital_min === '' ? null : Number(f.capital_min),
        capital_max: f.capital_max === '' ? null : Number(f.capital_max),
        nome: f.nome.trim() || null, uf: f.uf.trim() || null,
        abertura_de: f.abertura_de || null, abertura_ate: f.abertura_ate || null,
      }
      const r = await api.searchCnpjs(payload)
      const cnpjs = r.cnpjs || []
      if (aleatorio && !cnpjs.length && targetPage > 1) {
        return buscar(1, false)
      }
      setResults(cnpjs)
      setTotal(r.total || 0)
      lastTotalRef.current = r.total || 0
      setPage(r.pagina || targetPage)
      setPoolFiltrados(r.filtrados_pool || 0)
      if (!cnpjs.length) setMsg({ ok: false, text: 'Nenhum CNPJ encontrado com esses filtros.' })
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setBusy(null) }
  }

  const adicionarPool = async () => {
    if (!results.length) return
    setBusy('add'); setMsg(null)
    try {
      const text = results.map((r) => r.cnpj).join('\n')
      const r = await api.addCnpjsManual(text)
      setMsg({ ok: true, text: `${r.added} CNPJ(s) adicionado(s) ao pool (de ${r.parsed} encontrados).` })
      setResults([])
      onAdded?.()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setBusy(null) }
  }

  const copiar = () => {
    try { navigator.clipboard.writeText(results.map((r) => r.cnpj).join('\n')) } catch { /* bloqueado */ }
  }

  const Toggle = ({ k, label }) => (
    <button type="button" role="switch" aria-checked={f[k]} onClick={() => set(k, !f[k])}
      className="flex items-center gap-2 px-1.5 py-1">
      <span className={`relative h-5 w-9 shrink-0 rounded-full transition ${
        f[k] ? 'bg-gradient-to-r from-amber-300 to-emerald-400 shadow-[0_0_10px_-2px_rgba(52,211,153,0.7)]' : 'bg-neutral-300 dark:bg-neutral-700'
      }`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${f[k] ? 'left-[18px]' : 'left-0.5'}`} />
      </span>
      <span className={`text-xs font-bold uppercase tracking-wide ${
        f[k] ? 'text-emerald-600 dark:text-emerald-300' : 'text-neutral-500 dark:text-emerald-100/60'
      }`}>{label}</span>
    </button>
  )

  return (
    <div className="mt-4 rounded-xl border border-neutral-200 dark:border-emerald-400/20 p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="cyber-ibadge blue h-8 w-8"><ListPlus size={16} /></span>
        <h3 className="font-display text-sm font-bold uppercase tracking-wider text-neutral-900 dark:text-emerald-100">Gerar lista de CNPJs</h3>
        <span className="ml-auto text-[10px] uppercase tracking-wide text-neutral-400 dark:text-emerald-300/50">Casa dos Dados</span>
      </div>

      <div className="grid grid-cols-2 gap-2 mb-2">
        <input className={`${inputCls} col-span-2`} placeholder="Nome / razão social (opcional)" value={f.nome} onChange={(e) => set('nome', e.target.value)} />
        <input className={inputCls} type="number" min={1} max={1000} placeholder="Quantidade" value={f.quantidade} onChange={(e) => set('quantidade', e.target.value)} />
        <input className={inputCls} placeholder="UF (ex.: SP) — opcional" value={f.uf} onChange={(e) => set('uf', e.target.value)} />
        <input className={inputCls} type="number" placeholder="Capital social (de)" value={f.capital_min} onChange={(e) => set('capital_min', e.target.value)} />
        <input className={inputCls} type="number" placeholder="Capital social (até)" value={f.capital_max} onChange={(e) => set('capital_max', e.target.value)} />
        <label className="text-[10px] uppercase tracking-wide text-neutral-400 dark:text-emerald-300/50 flex flex-col gap-1">Abertura de
          <input className={inputCls} type="date" value={f.abertura_de} onChange={(e) => set('abertura_de', e.target.value)} />
        </label>
        <label className="text-[10px] uppercase tracking-wide text-neutral-400 dark:text-emerald-300/50 flex flex-col gap-1">Abertura até
          <input className={inputCls} type="date" value={f.abertura_ate} onChange={(e) => set('abertura_ate', e.target.value)} />
        </label>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 mb-3">
        <Toggle k="situacao_ativa" label="Ativa" />
        <Toggle k="somente_mei" label="MEI" />
        <Toggle k="somente_matriz" label="Matriz" />
      </div>

      <div className="flex flex-wrap gap-2">
        <button onClick={() => buscar(1, true)} disabled={busy} className={`${btnPrimary} px-4 py-2 text-sm flex items-center gap-2 disabled:opacity-50`}>
          <Search size={15} /> {busy === 'search' ? 'Buscando…' : 'Buscar'}
        </button>
        {results.length > 0 && (
          <>
            <button onClick={adicionarPool} disabled={busy} className={`${btnGhost} px-4 py-2 text-sm flex items-center gap-2`}>
              <ListPlus size={15} /> {busy === 'add' ? 'Adicionando…' : `Adicionar ${results.length} ao pool`}
            </button>
            <button onClick={copiar} className={`${btnGhost} px-3 py-2 text-sm flex items-center gap-2`} title="Copiar CNPJs">
              <Copy size={15} /> Copiar
            </button>
          </>
        )}
      </div>

      {msg && <p className={`mt-2 text-sm ${msg.ok ? 'text-emerald-600 dark:text-emerald-300 font-medium' : 'text-red-500'}`}>{msg.text}</p>}

      {results.length > 0 && (
        <div className="mt-3 flex items-center justify-between gap-2">
          <button onClick={() => buscar(page - 1)} disabled={busy || page <= 1}
            className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-1.5 text-xs font-bold uppercase tracking-wide hover:bg-neutral-100 dark:hover:bg-emerald-400/10 disabled:opacity-40 disabled:cursor-not-allowed">
            ◀ Anterior
          </button>
          <span className="text-[11px] text-neutral-500 dark:text-emerald-200/60 text-center">
            Página <b className="tabular-nums">{page}</b>/<b className="tabular-nums">{totalPages}</b> · <b className="tabular-nums">{total.toLocaleString('pt-BR')}</b> encontrados
            {poolFiltrados > 0 && <span className="text-amber-500 dark:text-amber-400"> · {poolFiltrados} já no pool</span>}
          </span>
          <button onClick={() => buscar(page + 1)} disabled={busy || page >= totalPages}
            className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-1.5 text-xs font-bold uppercase tracking-wide hover:bg-neutral-100 dark:hover:bg-emerald-400/10 disabled:opacity-40 disabled:cursor-not-allowed">
            Próxima ▶
          </button>
        </div>
      )}

      {results.length > 0 && (
        <div className="mt-2 max-h-64 overflow-y-auto rounded-lg border border-neutral-200 dark:border-emerald-400/15 divide-y divide-neutral-100 dark:divide-emerald-400/10">
          {results.map((r) => (
            <div key={r.cnpj} className="px-3 py-1.5 flex items-center justify-between gap-2 text-xs">
              <span className="font-mono font-bold text-neutral-800 dark:text-emerald-50">{r.cnpj}</span>
              <span className="truncate text-neutral-500 dark:text-emerald-200/60 flex-1 text-right">{r.razao_social || r.nome_fantasia || r.situacao}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Uploader({ label, hint, accept = '.csv,.xlsx,.txt', onUpload, onDone }) {
  const ref = useRef()
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const send = async (file) => {
    if (!file) return
    setBusy(true); setMsg(null)
    try {
      const res = await onUpload(file)
      setMsg({ ok: true, text: `${res.added} adicionado(s) de ${res.parsed}.${res.errors?.length ? ` ${res.errors.length} rejeitado(s).` : ''}`, errors: res.errors })
      onDone?.()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setBusy(false); if (ref.current) ref.current.value = '' }
  }
  return (
    <div>
      <p className="font-medium text-sm">{label}</p>
      <p className="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">{hint}</p>
      <input
        ref={ref} type="file" accept={accept} disabled={busy}
        onChange={(e) => send(e.target.files[0])}
        className="mt-2.5 block w-full text-sm text-neutral-600 dark:text-neutral-300 file:mr-3 file:rounded-lg file:border-0 file:bg-neutral-900 dark:file:bg-white file:text-white dark:file:text-neutral-900 file:px-3 file:py-1.5 file:font-medium file:cursor-pointer"
      />
      {msg && <p className={`mt-2 text-xs ${msg.ok ? 'text-neutral-700 dark:text-neutral-200 font-medium' : 'text-red-500'}`}>{msg.text}</p>}
      {msg?.errors?.length > 0 && (
        <ul className="mt-1 text-xs text-red-500 list-disc pl-4 space-y-0.5">
          {msg.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
    </div>
  )
}

// Entrada manual — cole uma MENSAGEM/bloco (1 por linha, ou vários colados de
// uma vez) sem precisar montar arquivo. Genérico: CNPJs, contas FB e proxies
// (onAdd = api.addXManual). Textarea p/ aceitar paste multi-linha.
// Cmd/Ctrl+Enter envia (Enter sozinho quebra linha, como esperado num paste).
function ManualAdd({ onAdd, onDone, placeholder, hint, rows = 2 }) {
  const [val, setVal] = useState('')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const add = async () => {
    if (!val.trim()) return
    setBusy(true); setMsg(null)
    try {
      const res = await onAdd(val)
      setMsg({ ok: true, text: `${res.added} adicionado(s) de ${res.parsed}.${res.errors?.length ? ` ${res.errors.length} rejeitado(s).` : ''}`, errors: res.errors })
      if (res.added) setVal('')
      onDone?.()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setBusy(false) }
  }
  return (
    <div className="mt-3 pt-3 border-t border-neutral-100 dark:border-neutral-800">
      <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-1.5">{hint}</p>
      <textarea
        value={val} onChange={(e) => setVal(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) add() }}
        placeholder={placeholder} disabled={busy} rows={rows}
        className={`${inputCls} w-full resize-y font-mono text-xs`}
      />
      <button onClick={add} disabled={busy || !val.trim()} className={`${btnPrimary} mt-2 px-4 py-1.5 text-sm`}>Adicionar</button>
      {msg && <p className={`mt-2 text-xs ${msg.ok ? 'text-neutral-700 dark:text-neutral-200 font-medium' : 'text-red-500'}`}>{msg.text}</p>}
      {msg?.errors?.length > 0 && (
        <ul className="mt-1 text-xs text-red-500 list-disc pl-4 space-y-0.5">
          {msg.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
    </div>
  )
}

// `idKey` liga a seleção (checkbox + "todos" + apagar) — sem ele, a lista
// fica como antes (só visualização, ex.: nenhum uso hoje exige isso, mas
// mantém compat). Checkbox e o clique-pra-ver (onItemClick, usado nos CNPJs)
// são IRMÃOS, não um dentro do outro — input dentro de button é HTML inválido
// e o clique do checkbox vazaria pro onClick do botão.
function PoolList({
  title, items, empty, render, onItemClick,
  idKey, selected, onToggle, onToggleAll, onDeleteSelected, deleteBusy,
}) {
  const allSelected = !!idKey && items.length > 0 && items.every((it) => selected?.has(it[idKey]))
  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <p className="text-xs font-semibold uppercase tracking-wide text-neutral-400">{title} ({items.length})</p>
        {idKey && items.length > 0 && (
          <div className="flex items-center gap-2.5">
            <label className="flex items-center gap-1 text-[11px] text-neutral-500 dark:text-neutral-400 cursor-pointer select-none">
              <input
                type="checkbox" checked={allSelected} onChange={() => onToggleAll(items)}
                className="h-3.5 w-3.5 accent-lime-600"
              />
              todos
            </label>
            <button
              type="button" onClick={onDeleteSelected} disabled={!selected?.size || deleteBusy}
              className="text-[11px] font-medium text-red-600 dark:text-red-400 hover:underline disabled:opacity-40 disabled:hover:no-underline disabled:cursor-not-allowed"
            >
              {deleteBusy ? 'apagando…' : `apagar${selected?.size ? ` (${selected.size})` : ''}`}
            </button>
          </div>
        )}
      </div>
      <div className="max-h-64 overflow-y-auto rounded-lg border border-neutral-100 dark:border-neutral-800 divide-y divide-neutral-100 dark:divide-neutral-800">
        {items.length === 0
          ? <p className="text-xs text-neutral-400 px-3 py-3">{empty}</p>
          : items.map((it, i) => {
              const id = idKey ? it[idKey] : null
              const content = <div className="flex-1 min-w-0 flex items-center justify-between gap-2">{render(it)}</div>
              return (
                <div key={i} className="px-3 py-2.5 text-sm flex items-center gap-2 hover:bg-neutral-50 dark:hover:bg-neutral-800/60">
                  {idKey && (
                    <input
                      type="checkbox" checked={!!selected?.has(id)} onChange={() => onToggle(id)}
                      className="h-3.5 w-3.5 accent-lime-600 shrink-0"
                    />
                  )}
                  {onItemClick
                    ? <button type="button" onClick={() => onItemClick(it)} className="flex-1 min-w-0 text-left">{content}</button>
                    : content}
                </div>
              )
            })}
      </div>
    </div>
  )
}

// Card de estatística — ícone em badge brilhante + número grande (KPI).
const _SERIAL = { green: 'CNPJ·07', blue: 'FB·07', violet: 'PRX·07', gold: 'SYS·07' }
function StatCard({ icon: Icon, label, avail, used, tone }) {
  return (
    <div className="cyber-card relative rounded-2xl border border-neutral-200 bg-white p-5">
      <span className="hud-corner tl" /><span className="hud-corner tr" />
      <span className="hud-corner bl" /><span className="hud-corner br" />
      <span className="hud-tag hidden dark:block absolute top-2.5 right-4">// {_SERIAL[tone] || 'X·07'}</span>
      <div className="flex items-center gap-3 mb-3">
        <div className={`cyber-ibadge ${tone} h-11 w-11 shrink-0`}><Icon size={20} /></div>
        <span className="text-sm font-bold uppercase tracking-wide text-neutral-700 dark:text-emerald-100/90">{label}</span>
      </div>
      <div className="flex items-end justify-between">
        <div>
          <div className="font-display cyber-kpi text-4xl font-black tabular-nums text-neutral-900 dark:text-white leading-none">{avail}</div>
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 dark:text-emerald-200/50 mt-1.5">disponíveis</div>
        </div>
        <div className="text-right">
          <div className="text-xl font-bold tabular-nums text-neutral-400 dark:text-emerald-200/40 leading-none">{used}</div>
          <div className="text-[11px] uppercase tracking-wider text-neutral-400 dark:text-emerald-200/30 mt-1.5">usados</div>
        </div>
      </div>
    </div>
  )
}
function StepHead({ n, icon: Icon, title, hint, tone = 'green' }) {
  return (
    <div className="flex items-center gap-3 mb-4">
      <div className={`cyber-ibadge ${tone} h-10 w-10 shrink-0 font-extrabold text-sm`}>
        {Icon ? <Icon size={18} /> : n}
      </div>
      <div>
        <h3 className="font-display text-base font-extrabold uppercase tracking-wide leading-tight text-neutral-900 dark:text-emerald-50 flex items-center gap-2">
          <span className="text-[11px] font-black text-amber-400/80">{n}</span> {title}
        </h3>
        {hint && <p className="text-xs text-neutral-500 dark:text-emerald-200/50">{hint}</p>}
      </div>
    </div>
  )
}

// ── SMS provider icons (inline SVG, platform branding) ──────────────────
const IconNV = () => (
  <svg className="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect width="24" height="24" rx="5" fill="#3b82f6"/>
    <path d="M7 17V7h2l5 7V7h2.5v10H14L9 10v7H7z" fill="white"/>
  </svg>
)
const IconSms24h = () => (
  <svg className="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect x="3.5" y="3.5" width="12" height="12" rx="2" transform="rotate(45 12 12)" fill="#f59e0b"/>
    <path d="M8.5 15.5l7-7m0 0H11m4.5 0V13" stroke="#1e40af" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
  </svg>
)
const IconSmsRush = () => (
  <svg className="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <path d="M3 10.5c0-3.6 4-6.5 9-6.5s9 2.9 9 6.5-4 6.5-9 6.5c-1.3 0-2.5-.2-3.5-.5L4 19l1.3-3.3C4 14.3 3 12.5 3 10.5z" fill="#f97316"/>
    <circle cx="8.5" cy="10.5" r="1.1" fill="white"/>
    <circle cx="12" cy="10.5" r="1.1" fill="white"/>
    <circle cx="15.5" cy="10.5" r="1.1" fill="white"/>
  </svg>
)

export default function Batch() {
  const [pools, setPools] = useState(null)
  const [count, setCount] = useState(1)
  const [proxyScheme, setProxyScheme] = useState('http')
  const [proxyType, setProxyType] = useState('datacenter')
  // Conexão: 'proxy' = usa o pool de proxies; 'direct' = 5G/VPN (conexão direta
  // da máquina, sem proxy). Padrão: Proxy (pool) (pedido do operador).
  const [connMode, setConnMode] = useState('proxy')
  // SMS: aba do provedor (nv / sms24h / smsrush) + sub-opção (NV only) + DDD.
  const [smsProvider, setSmsProvider] = useState('nv')
  const [smsSubOption, setSmsSubOption] = useState('facebook')
  const [smsDdd, setSmsDdd] = useState('')
  const [smsRushServer, setSmsRushServer] = useState('1')
  const [smsFallbackProvider, setSmsFallbackProvider] = useState('')
  const [dualBm, setDualBm] = useState(false)
  const [browserProvider, setBrowserProvider] = useState('adspower')
  const [wabaEnabled, setWabaEnabled] = useState(false)
  const [smsBal, setSmsBal] = useState({})
  const smsOption = smsProvider === 'nv' ? smsSubOption : smsProvider === 'sms24h' ? 'sms24h' : 'smsrush'
  const fallbackProviders = [
    { id: 'nv', label: 'Numero-Virtual', option: 'facebook' },
    { id: 'sms24h', label: 'SMS24H', option: 'sms24h' },
    { id: 'smsrush', label: 'SMSRush', option: 'smsrush' },
  ].filter((p) => p.id !== smsProvider)
  const smsFallbackOption = (fallbackProviders.find((p) => p.id === smsFallbackProvider) || {}).option || null
  // Piloto automático: ciclos sequenciais de runs simultâneas.
  const [autoOn, setAutoOn] = useState(false)
  const [autoTarget, setAutoTarget] = useState(3)
  const [autoCycle, setAutoCycle] = useState(0)
  const [autoTotal, setAutoTotal] = useState(3)
  const [feedback, setFeedback] = useState(null)
  const [confirming, setConfirming] = useState(false)
  // "Só logar": loga na conta e para em business.facebook.com — o resto é
  // manual (pedido do operador). Estado próprio, separado do disparo normal.
  const [confirmingLogin, setConfirmingLogin] = useState(false)
  const [loginBusy, setLoginBusy] = useState(false)
  const [loginFeedback, setLoginFeedback] = useState(null)
  const [confirmingAdAccount, setConfirmingAdAccount] = useState(false)
  const [adAccountFeedback, setAdAccountFeedback] = useState(null)
  const [selCnpj, setSelCnpj] = useState(null)
  const [cnpjMeta, setCnpjMeta] = useState({}) // cnpj -> {loading|data|err}

  // Seleção pra apagar do pool — 1 Set por categoria (CNPJs/contas/proxies),
  // chaveado pelo idKey de cada lista (cnpj/email/label).
  const [selCnpjs, setSelCnpjs] = useState(() => new Set())
  const [selAccounts, setSelAccounts] = useState(() => new Set())
  const [selProxies, setSelProxies] = useState(() => new Set())
  const [deletingCnpjs, setDeletingCnpjs] = useState(false)
  const [deletingAccounts, setDeletingAccounts] = useState(false)
  const [deletingProxies, setDeletingProxies] = useState(false)

  const toggleIn = (setFn) => (id) => setFn((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
  const toggleAllIn = (setFn, idKey) => (items) => setFn((prev) => {
    const allSelected = items.length > 0 && items.every((it) => prev.has(it[idKey]))
    return allSelected ? new Set() : new Set(items.map((it) => it[idKey]))
  })
  // Apaga os selecionados via `apiFn`, limpa a seleção e atualiza o pool.
  // Erro vira feedback visível (mesmo padrão do disparo) em vez de sumir.
  const deleteSelected = async (apiFn, selectedSet, setSelected, setBusy) => {
    if (!selectedSet.size) return
    const n = selectedSet.size
    if (!window.confirm(`Apagar ${n} ${n === 1 ? 'item' : 'itens'} selecionado(s) do pool? Não dá pra desfazer.`)) return
    setBusy(true)
    try {
      const result = await apiFn([...selectedSet])
      console.log('Delete result:', result)
      setSelected(new Set())
      setFeedback({ ok: true, text: `Deletado(s) ${result.deleted || n} item(ns).` })
      refresh()
    } catch (e) {
      console.error('Delete error:', e)
      setFeedback({ ok: false, text: `Erro ao deletar: ${e.message || String(e)}` })
    } finally {
      setBusy(false)
    }
  }

  const refreshSmsBal = () => {
    Promise.allSettled([
      api.smsBalance('numero_virtual'),
      api.smsBalance('sms24h_org'),
    ]).then(([nv, s24]) => {
      setSmsBal((p) => ({
        ...p,
        nv: nv.status === 'fulfilled' ? nv.value : p?.nv,
        sms24h: s24.status === 'fulfilled' ? s24.value : p?.sms24h,
      }))
    })
  }

  const refreshAutopilot = () => api.stats().then((s) => {
    if (s.autopilot) {
      setAutoOn(!!s.autopilot.enabled)
      setAutoTarget(s.autopilot.total_cycles || 3)
      setAutoCycle(s.autopilot.current_cycle || 0)
      setAutoTotal(s.autopilot.total_cycles || 3)
    }
  }).catch(() => {})
  const refresh = () => { api.pools().then(setPools).catch(() => {}); refreshAutopilot() }
  useEffect(() => { refresh(); refreshSmsBal() }, [])
  useEffect(() => {
    if (!autoOn) return
    const t = setInterval(refreshAutopilot, 5000)
    return () => clearInterval(t)
  }, [autoOn])



  // Liga/desliga o piloto automático (ciclos sequenciais de runs simultâneas).
  const applyAutopilot = async (enabled, totalCycles) => {
    try {
      const cycles = Math.max(1, Number(totalCycles) || 3)
      const runsPerCycle = Math.max(1, Number(count) || 1)
      const res = await api.setAutopilot(enabled, {
        runsPerCycle,
        totalCycles: cycles,
        useProxy: connMode === 'proxy',
        proxyType,
        smsOption: smsOption || null,
        smsDdd: smsDdd || null,
        smsrushServer: smsProvider === 'smsrush' ? smsRushServer : null,
        smsFallbackOption: smsFallbackOption || null,
        dualBm: dualBm,
        browserProvider,
        wabaEnabled,
      })
      setAutoOn(enabled)
      setAutoTarget(cycles)
      if (enabled) {
        setAutoCycle(1)
        setAutoTotal(cycles)
      } else {
        setAutoCycle(0)
      }
      const kicked = res?.kicked || 0
      setFeedback({
        ok: true,
        text: enabled
          ? `Piloto Automático LIGADO — ${cycles} ciclo(s) de ${runsPerCycle} run(s).${kicked ? ` Ciclo 1/${cycles} iniciado (${kicked} run${kicked === 1 ? '' : 's'}).` : ' Aguardando as runs atuais terminarem para iniciar o Ciclo 1.'}`
          : 'Piloto Automático desligado — ciclos interrompidos (runs em andamento seguem até o fim).',
      })
    } catch (e) { setFeedback({ ok: false, text: e.message }) }
  }

  const enqueue = async () => {
    setFeedback(null); setConfirming(false)
    try {
      const res = await api.enqueue(Number(count), connMode === 'proxy', proxyType, smsOption, smsDdd || null, smsProvider === 'smsrush' ? smsRushServer : null, smsFallbackOption, dualBm, browserProvider, wabaEnabled)
      setFeedback({ ok: true, text: `${res.enqueued} run(s) iniciada(s). Acompanhe em Runs & Logs.` })
      refresh()
    } catch (e) { setFeedback({ ok: false, text: e.message }) }
  }

  const enqueueAdAccount = async () => {
    setAdAccountFeedback(null); setConfirmingAdAccount(false)
    try {
      const res = await api.enqueue(Number(count), connMode === 'proxy', proxyType, smsOption, smsDdd || null, smsProvider === 'smsrush' ? smsRushServer : null, smsFallbackOption, dualBm, browserProvider, wabaEnabled, true)
      setAdAccountFeedback({ ok: true, text: `${res.enqueued} run(s) iniciada(s) (até conta de anúncios). Acompanhe em Runs & Logs.` })
      refresh()
    } catch (e) { setAdAccountFeedback({ ok: false, text: e.message }) }
  }

  // Só logar (embaixo de "Enfileirar e rodar"): abre a conta, loga e para em
  // business.facebook.com. Não consome CNPJ nenhum — só contas do estoque.
  const loginOnly = async () => {
    setLoginFeedback(null); setConfirmingLogin(false); setLoginBusy(true)
    try {
      const res = await api.loginOnly(Number(loginCount), connMode === 'proxy')
      setLoginFeedback({
        ok: true,
        text: res.enqueued > 0
          ? `${res.enqueued} conta(s) logando — acompanhe em Runs & Logs. Assim que logar, para sozinho em business.facebook.com.`
          : (res.warning || 'Nenhuma conta disponível no estoque.'),
      })
      refresh()
    } catch (e) { setLoginFeedback({ ok: false, text: e.message }) }
    finally { setLoginBusy(false) }
  }

  const c = pools?.counts || {}
  const maxPairs = Math.min(c.cnpjs_available || 0, c.accounts_available || 0)
  const cnpjs = (pools?.cnpjs || []).filter((x) => x.status === 'available')
  const accounts = (pools?.accounts || []).filter((x) => x.status === 'available')
  const maxLoginOnly = accounts.length
  const loginCount = Math.min(Number(count) || 0, maxLoginOnly) || (maxLoginOnly ? 1 : 0)
  const proxies = (pools?.proxies || []).filter((x) => x.status === 'available')
  const cnpjKey = cnpjs.map((x) => x.cnpj).join(',')

  // Assim que um CNPJ entra no pool, consulta os dados (sequencial p/ não
  // estourar o rate limit da BrasilAPI; o backend cacheia, então o run reusa).
  // Sem filtro de logradouro: fica no pool qualquer CNPJ que o operador colar.
  useEffect(() => {
    let cancelled = false
    const list = cnpjKey ? cnpjKey.split(',') : []
    ;(async () => {
      for (const cn of list) {
        if (cancelled) return
        let skip = false
        setCnpjMeta((m) => { if (m[cn] && !m[cn].loading) skip = true; return m })
        if (skip) continue
        setCnpjMeta((m) => ({ ...m, [cn]: { loading: true } }))
        try {
          const d = await api.cnpjInfo(cn)
          if (cancelled) return
          setCnpjMeta((m) => ({ ...m, [cn]: { data: d } }))
        } catch (e) {
          if (cancelled) return
          setCnpjMeta((m) => ({ ...m, [cn]: { err: e.message } }))
        }
      }
    })()
    return () => { cancelled = true }
  }, [cnpjKey])

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <StatCard icon={Database} label="CNPJs" avail={c.cnpjs_available || 0} used={c.cnpjs_used || 0} tone="green" />
        <StatCard icon={Users} label="Contas Facebook" avail={c.accounts_available || 0} used={c.accounts_used || 0} tone="blue" />
        <StatCard icon={Wifi} label="Proxies" avail={c.proxies_available || 0} used={c.proxies_used || 0} tone="violet" />
      </div>

      <div className="hidden dark:flex items-center gap-3">
        <span className="hud-tag text-emerald-300/70">▶▶ DEPLOY CONSOLE</span>
        <div className="hud-hazard flex-1" />
        <span className="hud-tag text-amber-300/70">// AREA 07</span>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5 items-start">
        {/* Esquerda: pools (upload + prévia) */}
        <div className="xl:col-span-2 space-y-5">
          <Card>
            <StepHead n="1" icon={Database} title="Suba os pools" hint="CNPJs, contas do Facebook e (se usar) proxies — por arquivo ou colando." />
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-3">
              <p className="text-sm font-bold mb-3 flex items-center gap-2"><span className="cyber-ibadge green h-7 w-7"><Database size={14} /></span> CNPJs</p>
              <Uploader label="CNPJs" hint="Um CNPJ por linha. Arquivo .txt, ou .csv/.xlsx com uma coluna chamada “cnpj”." onUpload={api.uploadCnpjs} onDone={refresh} /><ManualAdd onAdd={api.addCnpjsManual} onDone={refresh} placeholder={"00.000.000/0000-00\n11.111.111/1111-11"} hint="ou cole aqui — um CNPJ por linha (pode colar vários)" /></div>
            <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-3">
              <p className="text-sm font-bold mb-3 flex items-center gap-2"><span className="cyber-ibadge blue h-7 w-7"><Users size={14} /></span> Contas do Facebook</p>
              <Uploader label="Contas do Facebook" hint="Uma conta por linha: LOGIN SENHA [2FA] [EMAIL EMAILSENHA]. 2FA, email e senha do email são OPCIONAIS — só LOGIN+SENHA já basta. Login pode ser telefone OU email. Aceita : | ; TAB ou 2+ espaços (colar de planilha). Arquivo .txt, .csv ou .xlsx." onUpload={api.uploadAccounts} onDone={refresh} /><ManualAdd onAdd={api.addAccountsManual} onDone={refresh} rows={3} placeholder={"login  senha  (2fa, email e senha do email são opcionais)\n61594732979688  0786Poiu@$  R7WL JVRD P25B GJIO\ndevonuchanan@op.pl  Samii@1234  devonuchanan@op.pl  khan567996"} hint="ou cole aqui — LOGIN SENHA [2FA] [EMAIL EMAILSENHA]. Só login+senha já basta (2FA opcional)." /></div>
            <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-3">
              <p className="text-sm font-bold mb-3 flex items-center gap-2"><span className="cyber-ibadge violet h-7 w-7"><Wifi size={14} /></span> Conexão</p>
              <div className="grid grid-cols-2 gap-2 mb-1.5">
                <label className="block text-xs text-neutral-500 dark:text-neutral-400">
                  Conexão
                  <select
                    value={connMode} onChange={(e) => setConnMode(e.target.value)}
                    className={`${inputCls} mt-1 w-full text-sm`}
                  >
                    <option value="proxy">Proxy do pool (4G / residencial / datacenter)</option>
                    <option value="direct">Conexão da máquina (5G / VPN, sem proxy)</option>
                  </select>
                </label>
                <label className={`block text-xs text-neutral-500 dark:text-neutral-400 ${connMode === 'direct' ? 'opacity-40' : ''}`}>
                  Protocolo
                  <select
                    value={proxyScheme} onChange={(e) => setProxyScheme(e.target.value)}
                    disabled={connMode === 'direct'}
                    className={`${inputCls} mt-1 w-full text-sm`}
                  >
                    <option value="http">HTTP</option>
                    <option value="https">HTTPS</option>
                    <option value="socks5">SOCKS5</option>
                  </select>
                </label>
              </div>
              <div className={`grid gap-2 mb-1.5 ${connMode === 'direct' ? 'opacity-40' : ''}`}>
                <label className="block text-xs text-neutral-500 dark:text-neutral-400">
                  Tipo de proxy
                  <select
                    value={proxyType} onChange={(e) => setProxyType(e.target.value)}
                    disabled={connMode === 'direct'}
                    className={`${inputCls} mt-1 w-full text-sm`}
                  >
                    <option value="datacenter">Datacenter</option>
                    <option value="residential">Residencial (rotativo)</option>
                    <option value="mobile">Mobile (rotativo)</option>
                  </select>
                </label>
              </div>
              {connMode === 'direct' ? (
                <div className="text-xs rounded-lg border border-sky-300 dark:border-sky-900 bg-sky-50 dark:bg-sky-950/40 p-2.5 text-sky-700 dark:text-sky-400">
                  Modo <b>conexão da máquina</b>: as runs usam a internet do próprio PC (sua VPN/5G ou o OVPN do provedor instalado). <b>Não usa proxy do pool</b> — não precisa subir proxies. Use este SÓ se instalou a conexão na máquina.
                </div>
              ) : (
                <div className="text-xs rounded-lg border border-emerald-300 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/40 p-2.5 text-emerald-700 dark:text-emerald-400 mb-2">
                  Modo <b>proxy do pool</b>: cole seus proxies abaixo (inclusive <b>4G/mobile</b> como o da proxybrazil — <b>é aqui que ele vai</b>). Cada run pega um proxy do pool. "Tipo" é só rótulo.
                </div>
              )}
              {connMode !== 'direct' && (
                <>
                  <Uploader
                    label="Proxies" hint="Um proxy por linha: host:porta ou host:porta:usuário:senha. Arquivo .txt ou .csv. (use scheme://user:pass@host:port pra sobrescrever o tipo por linha)"
                    accept=".txt,.csv" onUpload={(file) => api.uploadProxies(file, proxyScheme)} onDone={refresh}
                  />
                  <ManualAdd
                    onAdd={(text) => api.addProxiesManual(text, proxyScheme)} onDone={refresh}
                    placeholder={"host:porta:usuário:senha\nhost:porta"}
                    hint={`ou cole aqui — um proxy por linha (host:porta:usuário:senha), tipo: ${proxyScheme.toUpperCase()}`}
                  />
                </>
              )}
            </div>
            </div>
          </Card>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <Card>
              <PoolList
                title="CNPJs no pool" items={cnpjs} empty="Nenhum CNPJ. Suba acima."
                onItemClick={(it) => setSelCnpj(it.cnpj)}
                idKey="cnpj" selected={selCnpjs} deleteBusy={deletingCnpjs}
                onToggle={toggleIn(setSelCnpjs)} onToggleAll={toggleAllIn(setSelCnpjs, 'cnpj')}
                onDeleteSelected={() => deleteSelected(api.deleteCnpjs, selCnpjs, setSelCnpjs, setDeletingCnpjs)}
                render={(it) => {
                  const meta = cnpjMeta[it.cnpj]
                  return (
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-sm">{it.cnpj}</span>
                        <span className="text-xs text-neutral-400 group-hover:text-neutral-600 shrink-0">ver dados ›</span>
                      </div>
                      {meta?.loading && <div className="text-xs text-neutral-400 mt-1">consultando dados…</div>}
                      {meta?.err && <div className="text-xs text-red-500 mt-1 truncate">{meta.err}</div>}
                      {meta?.data && (
                        <div className="mt-1 space-y-0.5">
                          <p className="text-sm text-neutral-700 dark:text-neutral-200 truncate">{meta.data.razao_social || 'sem razão social'}</p>
                          <p className="text-xs">
                            {meta.data.telefone
                              ? <span className="text-neutral-500 dark:text-neutral-400">tel. <span className="font-medium text-neutral-700 dark:text-neutral-200">{meta.data.telefone}</span></span>
                              : <span className="text-amber-600 dark:text-amber-400">sem telefone — puxar manual</span>}
                          </p>
                        </div>
                      )}
                    </div>
                  )
                }}
              />
              {selCnpj && <CnpjCard cnpj={selCnpj} onClose={() => setSelCnpj(null)} />}
            </Card>
            <Card>
              <PoolList
                title="Contas no pool" items={accounts} empty="Nenhuma conta. Suba acima."
                idKey="email" selected={selAccounts} deleteBusy={deletingAccounts}
                onToggle={toggleIn(setSelAccounts)} onToggleAll={toggleAllIn(setSelAccounts, 'email')}
                onDeleteSelected={() => deleteSelected(api.deleteAccounts, selAccounts, setSelAccounts, setDeletingAccounts)}
                render={(it) => <><span className="truncate">{it.email}</span><span className="text-neutral-400">disponível</span></>}
              />
            </Card>
            <Card>
              <PoolList
                title="Proxies no pool" items={proxies} empty="Nenhum proxy. Suba acima."
                idKey="label" selected={selProxies} deleteBusy={deletingProxies}
                onToggle={toggleIn(setSelProxies)} onToggleAll={toggleAllIn(setSelProxies, 'label')}
                onDeleteSelected={() => deleteSelected(api.deleteProxies, selProxies, setSelProxies, setDeletingProxies)}
                render={(it) => <><span className="font-mono truncate">{it.label}{it.username ? ' 🔒' : ''}</span><span className="text-neutral-400 uppercase">{it.scheme}</span></>}
              />
            </Card>
          </div>
        </div>

        {/* Direita: painel de disparo */}
        <Card className="xl:col-span-1 xl:sticky xl:top-6 border-2 border-lime-300 dark:border-lime-900 bg-gradient-to-br from-lime-50 to-emerald-50 dark:from-lime-950/20 dark:to-emerald-950/10">
          <StepHead n="3" icon={Rocket} title="Disparar" hint={`Qualquer conta pareia com qualquer CNPJ. Até ${maxPairs} par(es) agora.`} />
          <div className="grid gap-3">
            {maxPairs > 0 ? (
              <label className="text-sm">
                <span className="block text-neutral-500 dark:text-neutral-400 text-xs mb-1">Quantas runs (até {maxPairs})</span>
                <input type="number" min={1} max={maxPairs} value={count}
                  onChange={(e) => setCount(e.target.value)} className={inputCls} />
              </label>
            ) : (
              <div className="text-xs rounded-lg border border-amber-300 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/40 p-2.5 text-amber-700 dark:text-amber-400">
                Sem pares: você tem <b>{c.accounts_available || 0}</b> conta(s) e <b>{c.cnpjs_available || 0}</b> CNPJ(s). O nº de runs = mín(contas, CNPJs). Adicione {!(c.cnpjs_available) ? 'CNPJs' : 'contas'} ao pool acima para liberar.
              </div>
            )}
            {/* Toggle Multi-login: AdsPower / ixBrowser */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-500 dark:text-neutral-400">Multi-login</span>
              <div className="relative inline-flex rounded-full bg-neutral-200 dark:bg-neutral-800 p-0.5">
                <span className={`absolute top-0.5 bottom-0.5 w-[calc(50%-2px)] rounded-full transition-all duration-200 ${
                  browserProvider === 'ixbrowser'
                    ? 'left-[calc(50%+2px)] bg-gradient-to-r from-amber-500 to-orange-500 shadow-[0_0_8px_rgba(245,158,11,0.3)]'
                    : 'left-0.5 bg-gradient-to-r from-emerald-500 to-green-500 shadow-[0_0_8px_rgba(16,185,129,0.3)]'
                }`} />
                <button type="button" onClick={() => setBrowserProvider('adspower')}
                  className={`relative z-10 px-3 py-1 text-[11px] font-bold tracking-wide rounded-full transition-colors ${
                    browserProvider === 'adspower' ? 'text-white' : 'text-neutral-400 dark:text-neutral-500'
                  }`}>
                  ADSPOWER
                </button>
                <button type="button" onClick={() => setBrowserProvider('ixbrowser')}
                  className={`relative z-10 px-3 py-1 text-[11px] font-bold tracking-wide rounded-full transition-colors ${
                    browserProvider === 'ixbrowser' ? 'text-white' : 'text-neutral-400 dark:text-neutral-500'
                  }`}>
                  IXBROWSER
                </button>
              </div>
            </div>

            {/* Toggle 1BM / 2BM */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-500 dark:text-neutral-400">BMs por perfil</span>
              <div className="relative inline-flex rounded-full bg-neutral-200 dark:bg-neutral-800 p-0.5">
                <span className={`absolute top-0.5 bottom-0.5 w-[calc(50%-2px)] rounded-full transition-all duration-200 ${
                  dualBm
                    ? 'left-[calc(50%+2px)] bg-gradient-to-r from-lime-500 to-emerald-500 shadow-[0_0_10px_rgba(132,204,22,0.4)]'
                    : 'left-0.5 bg-gradient-to-r from-neutral-500 to-neutral-400 dark:from-neutral-500 dark:to-neutral-400'
                }`} />
                <button type="button" onClick={() => setDualBm(false)}
                  className={`relative z-10 px-3.5 py-1 text-[11px] font-bold tracking-wide rounded-full transition-colors ${
                    !dualBm ? 'text-white' : 'text-neutral-400 dark:text-neutral-500'
                  }`}>
                  1 BM
                </button>
                <button type="button" onClick={() => setDualBm(true)}
                  className={`relative z-10 px-3.5 py-1 text-[11px] font-bold tracking-wide rounded-full transition-colors ${
                    dualBm ? 'text-white' : 'text-neutral-400 dark:text-neutral-500'
                  }`}>
                  2 BM
                </button>
              </div>
            </div>

            {/* Toggle WABA ON / OFF */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-500 dark:text-neutral-400">WABA</span>
              <div className="relative inline-flex rounded-full bg-neutral-200 dark:bg-neutral-800 p-0.5">
                <span className={`absolute top-0.5 bottom-0.5 w-[calc(50%-2px)] rounded-full transition-all duration-200 ${
                  wabaEnabled
                    ? 'left-[calc(50%+2px)] bg-gradient-to-r from-emerald-500 to-teal-500 shadow-[0_0_10px_rgba(16,185,129,0.4)]'
                    : 'left-0.5 bg-gradient-to-r from-neutral-500 to-neutral-400 dark:from-neutral-500 dark:to-neutral-400'
                }`} />
                <button type="button" onClick={() => setWabaEnabled(false)}
                  className={`relative z-10 px-3.5 py-1 text-[11px] font-bold tracking-wide rounded-full transition-colors ${
                    !wabaEnabled ? 'text-white' : 'text-neutral-400 dark:text-neutral-500'
                  }`}>
                  OFF
                </button>
                <button type="button" onClick={() => setWabaEnabled(true)}
                  className={`relative z-10 px-3.5 py-1 text-[11px] font-bold tracking-wide rounded-full transition-colors ${
                    wabaEnabled ? 'text-white' : 'text-neutral-400 dark:text-neutral-500'
                  }`}>
                  ON
                </button>
              </div>
            </div>

            {/* SMS — saldo + provedor (aba) + opção + DDD/servidor */}
            <div>
              {/* ── Saldo SMS ── */}
              <div className="rounded-lg border border-neutral-200 dark:border-neutral-700 bg-neutral-50/60 dark:bg-neutral-900/40 p-2.5 mb-3">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-neutral-400 dark:text-neutral-500 flex items-center gap-1">
                    <Wallet size={11} /> Saldo SMS
                  </span>
                  <button type="button" onClick={refreshSmsBal}
                    className="text-[10px] text-neutral-400 hover:text-lime-500 dark:hover:text-lime-400 transition-colors">↻ atualizar</button>
                </div>
                <div className="grid grid-cols-3 gap-1.5">
                  {[
                    { key: 'nv', label: 'NV', bal: smsBal.nv },
                    { key: 'sms24h', label: '24H', bal: smsBal.sms24h },
                    { key: 'smsrush', label: 'Rush', bal: null },
                  ].map((p) => (
                    <div key={p.key} className="flex flex-col items-center rounded-md bg-white/70 dark:bg-neutral-800/60 px-1.5 py-1 border border-neutral-100 dark:border-neutral-700/50">
                      <span className="text-[8px] text-neutral-400 dark:text-neutral-500">{p.label}</span>
                      <span className={`text-xs font-bold tabular-nums ${
                        p.bal?.balance > 2 ? 'text-emerald-600 dark:text-emerald-400'
                        : p.bal?.balance > 0 ? 'text-amber-600 dark:text-amber-400'
                        : p.bal?.balance != null ? 'text-red-500 dark:text-red-400'
                        : 'text-neutral-300 dark:text-neutral-600'
                      }`}>
                        {p.bal?.balance != null ? `R$${p.bal.balance.toFixed(2)}` : '—'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              <span className="block text-neutral-500 dark:text-neutral-400 text-xs mb-1.5 flex items-center gap-1.5">
                <Smartphone size={13} /> Número SMS — escolha a fonte
              </span>
              {/* ── tab bar ── */}
              <div className="flex">
                {[
                  { id: 'nv',      icon: <IconNV />,      label: 'Numero-Virtual', c: 'blue'  },
                  { id: 'sms24h',  icon: <IconSms24h />,  label: 'SMS24H',         c: 'amber' },
                  { id: 'smsrush', icon: <IconSmsRush />, label: 'SMSRush',        c: 'teal'  },
                ].map((t) => {
                  const on = smsProvider === t.id
                  const cls = {
                    blue:  'border-blue-400/50 bg-blue-500/10 text-blue-300 shadow-[0_-2px_12px_-4px_rgba(59,130,246,0.35)]',
                    amber: 'border-amber-400/50 bg-amber-500/10 text-amber-300 shadow-[0_-2px_12px_-4px_rgba(245,158,11,0.35)]',
                    teal:  'border-teal-400/50 bg-teal-500/10 text-teal-300 shadow-[0_-2px_12px_-4px_rgba(20,184,166,0.35)]',
                  }[t.c]
                  return (
                    <button key={t.id} type="button" onClick={() => setSmsProvider(t.id)}
                      className={`flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-bold uppercase tracking-wide rounded-t-lg border border-b-0 transition-all ${
                        on ? `${cls} relative z-10` : 'border-transparent text-neutral-500 hover:text-neutral-300'
                      }`}>
                      {t.icon}
                      <span className="hidden sm:inline">{t.label}</span>
                      <span className="sm:hidden">{t.id === 'nv' ? 'NV' : t.id === 'sms24h' ? '24H' : 'Rush'}</span>
                    </button>
                  )
                })}
              </div>
              {/* ── tab content ── */}
              <div className={`rounded-b-lg rounded-tr-lg border p-3 -mt-px transition-colors ${
                smsProvider === 'nv' ? 'border-blue-400/50' : smsProvider === 'sms24h' ? 'border-amber-400/50' : 'border-teal-400/50'
              }`}>
                {/* ── Numero-Virtual ── */}
                {smsProvider === 'nv' && (<>
                  <label className="flex items-center gap-2 text-[11px] text-neutral-500 dark:text-neutral-400 mb-2.5">
                    DDD
                    <input type="text" inputMode="numeric" maxLength={2} value={smsDdd}
                      onChange={(e) => setSmsDdd(e.target.value.replace(/\D/g, ''))}
                      placeholder="vazio = aleatório" className={`${inputCls} w-32 ml-auto text-xs`} />
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <button type="button" onClick={() => setSmsSubOption('facebook')}
                      className={`rounded-lg border-2 px-3 py-2 text-left transition ${smsSubOption === 'facebook'
                        ? 'border-emerald-400 bg-emerald-500/15 shadow-[0_0_12px_-4px_rgba(52,211,153,0.5)]'
                        : 'border-neutral-300 dark:border-neutral-700 hover:border-emerald-400/40 opacity-70'}`}>
                      <span className="block font-bold text-sm">Facebook</span>
                      <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">Vivo · R$1,30</span>
                    </button>
                    <button type="button" onClick={() => setSmsSubOption('other_1')}
                      className={`rounded-lg border-2 px-3 py-2 text-left transition ${smsSubOption === 'other_1'
                        ? 'border-sky-400 bg-sky-400/15 shadow-[0_0_12px_-4px_rgba(56,189,248,0.5)]'
                        : 'border-neutral-300 dark:border-neutral-700 hover:border-sky-400/40 opacity-70'}`}>
                      <span className="block font-bold text-sm">Outros Op.1</span>
                      <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">aleat. · R$2,00</span>
                    </button>
                    <button type="button" onClick={() => setSmsSubOption('other_2')}
                      className={`rounded-lg border-2 px-3 py-2 text-left transition ${smsSubOption === 'other_2'
                        ? 'border-amber-400 bg-amber-400/15 shadow-[0_0_12px_-4px_rgba(251,191,36,0.5)]'
                        : 'border-neutral-300 dark:border-neutral-700 hover:border-amber-400/40 opacity-70'}`}>
                      <span className="block font-bold text-sm">Outros Op.2</span>
                      <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">aleat. · R$1,80</span>
                    </button>
                    <button type="button" onClick={() => setSmsSubOption('other_3')}
                      className={`rounded-lg border-2 px-3 py-2 text-left transition ${smsSubOption === 'other_3'
                        ? 'border-rose-400 bg-rose-400/15 shadow-[0_0_12px_-4px_rgba(251,113,133,0.5)]'
                        : 'border-neutral-300 dark:border-neutral-700 hover:border-rose-400/40 opacity-70'}`}>
                      <span className="block font-bold text-sm">Outros Op.3</span>
                      <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">aleat. · R$2,70</span>
                    </button>
                  </div>
                </>)}
                {/* ── SMS24H ── */}
                {smsProvider === 'sms24h' && (<>
                  <label className="flex items-center gap-2 text-[11px] text-neutral-500 dark:text-neutral-400 mb-2.5">
                    DDD
                    <input type="text" inputMode="numeric" maxLength={2} value={smsDdd}
                      onChange={(e) => setSmsDdd(e.target.value.replace(/\D/g, ''))}
                      placeholder="vazio = aleatório" className={`${inputCls} w-32 ml-auto text-xs`} />
                  </label>
                  <div className="rounded-lg border-2 border-amber-400 bg-amber-400/10 px-3 py-2">
                    <span className="block font-bold text-sm">Facebook</span>
                    <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">sms24h.org · R$0,80</span>
                  </div>
                </>)}
                {/* ── SMSRush ── */}
                {smsProvider === 'smsrush' && (<>
                  <label className="block text-[11px] text-neutral-500 dark:text-neutral-400 mb-2.5">
                    Servidor
                    <select value={smsRushServer} onChange={(e) => setSmsRushServer(e.target.value)}
                      className={`${inputCls} mt-1 w-full text-sm`}>
                      <option value="1">Servidor 1</option>
                      <option value="2">Servidor 2</option>
                      <option value="3">Servidor 3</option>
                      <option value="4">Servidor 4</option>
                    </select>
                  </label>
                  <div className="rounded-lg border-2 border-teal-400 bg-teal-400/10 px-3 py-2">
                    <span className="block font-bold text-sm">Facebook</span>
                    <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">smsrush.com.br · R$0,80</span>
                  </div>
                </>)}
              </div>
              {/* 2ª Provedora — fallback SMS */}
              <div className="mt-2.5 rounded-lg border border-neutral-300 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-900/40 p-2.5">
                <label className="flex items-center gap-2 text-[11px] text-neutral-500 dark:text-neutral-400">
                  <RefreshCw size={12} />
                  2ª Provedora (se o SMS não chegar)
                  <select value={smsFallbackProvider} onChange={(e) => setSmsFallbackProvider(e.target.value)}
                    className={`${inputCls} ml-auto w-36 text-xs`}>
                    <option value="">Nenhuma</option>
                    {fallbackProviders.map((p) => (
                      <option key={p.id} value={p.id}>{p.label}</option>
                    ))}
                  </select>
                </label>
                {smsFallbackProvider && (
                  <p className="text-[10px] text-neutral-400 dark:text-neutral-500 mt-1.5">
                    Se o 1º número não receber SMS (2 tentativas × 2 min), troca automaticamente para <b>{fallbackProviders.find((p) => p.id === smsFallbackProvider)?.label}</b>. Se também falhar → encerra com SMS/ERRO2.
                  </p>
                )}
              </div>
              <p className="text-[10px] text-neutral-400 mt-1">Vale pra esta leva. Se um não recebe SMS, tente o outro.</p>
            </div>

            {confirming ? (
              <div className="rounded-lg border border-neutral-300 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-950 p-3">
                <p className="text-sm font-medium mb-2">
                  Disparar <b>{Math.min(Number(count) || 0, maxPairs)}</b> run(s) agora?
                </p>
                <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-2">
                  Número SMS: <b>{
                    smsOption === 'facebook' ? 'Numero-Virtual · Facebook (R$1,30)' :
                    smsOption === 'other_1' ? 'Numero-Virtual · Outros (Op.1 · R$2,00)' :
                    smsOption === 'other_2' ? 'Numero-Virtual · Outros (Op.2 · R$1,80)' :
                    smsOption === 'other_3' ? 'Numero-Virtual · Outros (Op.3 · R$2,70)' :
                    smsOption === 'sms24h' ? 'SMS24H · Facebook (R$0,80)' :
                    smsOption === 'smsrush' ? `SMSRush · Facebook · Servidor ${smsRushServer}` :
                    'Numero-Virtual · Facebook (R$1,30)'
                  }</b>
                  {smsDdd && <> · DDD <b>{smsDdd}</b></>}
                  {smsFallbackOption && <> · 2ª: <b>{fallbackProviders.find((p) => p.id === smsFallbackProvider)?.label}</b></>}
                </p>
                <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-2">
                  Multi-login: <b>{browserProvider === 'ixbrowser' ? 'ixBrowser' : 'AdsPower'}</b> · Modo: <b>{dualBm ? '2 BMs' : '1 BM'}</b>
                </p>
                <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-3">
                  Consome {Math.min(Number(count) || 0, maxPairs)} conta(s) + CNPJ(s) do pool e abre janelas no AdsPower. Não dá pra desfazer em lote.
                </p>
                <div className="flex gap-2">
                  <button onClick={enqueue} className={`${btnPrimary} px-4 py-2 text-sm flex-1`}>Confirmar disparo</button>
                  <button onClick={() => setConfirming(false)} className={`${btnGhost} px-4 py-2 text-sm`}>Cancelar</button>
                </div>
              </div>
            ) : (
              <button onClick={() => setConfirming(true)} disabled={!maxPairs} className={`${btnPrimary} px-5 py-3 font-semibold flex items-center justify-center gap-2`}>
                <Rocket size={17} /> Enfileirar e rodar
              </button>
            )}

            {confirmingAdAccount ? (
              <div className="rounded-lg border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 p-3">
                <p className="text-sm font-medium mb-2">
                  Disparar <b>{Math.min(Number(count) || 0, maxPairs)}</b> run(s) até a <b>Conta de Anúncios</b>?
                </p>
                <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-3">
                  Roda todo o fluxo (portfólio, domínio, dados, conta de anúncios) e para. Sem verificação da empresa, sem WABA. A run é marcada como 100% concluída nesse modo.
                </p>
                <div className="flex gap-2">
                  <button onClick={enqueueAdAccount} className="rounded-lg bg-amber-600 hover:bg-amber-700 text-white px-4 py-2 text-sm flex-1">Confirmar</button>
                  <button onClick={() => setConfirmingAdAccount(false)} className={`${btnGhost} px-4 py-2 text-sm`}>Cancelar</button>
                </div>
              </div>
            ) : (
              <button
                type="button" onClick={() => setConfirmingAdAccount(true)} disabled={!maxPairs}
                title="Roda todo o fluxo até criar a conta de anúncios e para — sem verificação da empresa."
                className="rounded-lg border border-amber-400 dark:border-amber-700 text-amber-700 dark:text-amber-400 px-5 py-2.5 text-sm font-medium disabled:opacity-50 flex items-center justify-center gap-2"
              >
                <Target size={15} /> Até Conta de Anúncios
              </button>
            )}
            {adAccountFeedback && <p className={`text-sm ${adAccountFeedback.ok ? 'text-amber-700 dark:text-amber-400 font-medium' : 'text-red-500'}`}>{adAccountFeedback.text}</p>}

            {confirmingLogin ? (
              <div className="rounded-lg border border-sky-300 dark:border-sky-800 bg-sky-50 dark:bg-sky-950/40 p-3">
                <p className="text-sm font-medium mb-2">
                  Só logar em <b>{loginCount}</b> conta(s) e parar?
                </p>
                <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-3">
                  Abre o AdsPower, loga na conta, entra em business.facebook.com e PARA — sem CNPJ, sem site, sem WhatsApp. Fica aberto pra você continuar à mão (igual "Assumir").
                </p>
                <div className="flex gap-2">
                  <button onClick={loginOnly} disabled={loginBusy} className="rounded-lg bg-sky-600 hover:bg-sky-700 text-white px-4 py-2 text-sm flex-1 disabled:opacity-50">
                    {loginBusy ? 'Logando…' : 'Confirmar'}
                  </button>
                  <button onClick={() => setConfirmingLogin(false)} className={`${btnGhost} px-4 py-2 text-sm`}>Cancelar</button>
                </div>
              </div>
            ) : (
              <button
                type="button" onClick={() => setConfirmingLogin(true)} disabled={!maxLoginOnly}
                title="Loga na conta e para em business.facebook.com — o resto (empresa, domínio, WhatsApp) fica manual."
                className="rounded-lg border border-sky-400 dark:border-sky-700 text-sky-700 dark:text-sky-400 px-5 py-2.5 text-sm font-medium disabled:opacity-50 flex items-center justify-center gap-2"
              >
                <LogIn size={15} /> Só logar e parar (business.facebook.com)
              </button>
            )}
            <button onClick={refresh} className={`${btnGhost} px-3 py-2 text-sm flex items-center justify-center gap-2`}><RefreshCw size={14} /> Atualizar</button>
          </div>
          {feedback && <p className={`mt-3 text-sm ${feedback.ok ? 'text-neutral-700 dark:text-neutral-200 font-medium' : 'text-red-500'}`}>{feedback.text}</p>}
          {loginFeedback && <p className={`mt-1 text-sm ${loginFeedback.ok ? 'text-sky-700 dark:text-sky-400 font-medium' : 'text-red-500'}`}>{loginFeedback.text}</p>}

          {/* Piloto Automático (CICLOS SEQUENCIAIS) */}
          <div className={`mt-4 rounded-xl border p-4 transition ${
            autoOn
              ? 'border-emerald-400/60 bg-emerald-50/60 dark:bg-emerald-400/[0.06] dark:border-emerald-400/50 shadow-[0_0_24px_-8px_rgba(52,211,153,0.6)]'
              : 'border-neutral-200 dark:border-emerald-400/15'
          }`}>
            <div className="flex items-center justify-between gap-3">
              <span className="font-display text-sm font-bold uppercase tracking-wider flex items-center gap-2 text-neutral-900 dark:text-emerald-100">
                <span className={`h-2.5 w-2.5 rounded-full transition ${
                  autoOn
                    ? 'bg-emerald-400 animate-pulse shadow-[0_0_10px_2px_rgba(52,211,153,0.85)]'
                    : 'bg-neutral-400 dark:bg-neutral-600'
                }`} />
                <Gauge size={16} className={autoOn ? 'text-emerald-500' : 'text-neutral-400'} /> Piloto Automático
              </span>
              <div className="flex items-center gap-2.5">
                <span className={`text-[11px] font-bold uppercase tracking-wide ${autoOn ? 'text-emerald-600 dark:text-emerald-300' : 'text-neutral-400 dark:text-neutral-500'}`}>
                  {autoOn ? 'Ligado' : 'Desligado'}
                </span>
                <button
                  type="button" role="switch" aria-checked={autoOn}
                  onClick={() => applyAutopilot(!autoOn, autoTarget)}
                  className={`relative h-7 w-14 rounded-full transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 ${
                    autoOn
                      ? 'bg-gradient-to-r from-amber-300 to-emerald-400 shadow-[0_0_16px_-2px_rgba(52,211,153,0.75)]'
                      : 'bg-neutral-300 dark:bg-neutral-700'
                  }`}
                >
                  <span className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow-md transition-all ${autoOn ? 'left-8' : 'left-1'}`} />
                </button>
              </div>
            </div>

            <label className="mt-3 text-xs text-neutral-500 dark:text-emerald-200/70 flex items-center gap-2 font-semibold uppercase tracking-wide">
              Ciclos
              <input
                type="number" min={1} max={50} value={autoTarget}
                onChange={(e) => setAutoTarget(e.target.value)}
                onBlur={() => autoOn && applyAutopilot(true, autoTarget)}
                className={`${inputCls} w-20`}
              />
              sequenciais
            </label>

            {autoOn && (
              <div className="mt-3 flex items-center gap-3 text-xs">
                <span className="font-semibold text-emerald-700 dark:text-emerald-300">
                  Ciclo {autoCycle}/{autoTotal}
                </span>
                <span className="text-neutral-400 dark:text-emerald-200/50">
                  Runs por ciclo: {Number(count) || 1}
                </span>
                <div className="flex-1 h-1.5 rounded-full bg-neutral-200 dark:bg-neutral-700 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-emerald-400 transition-all"
                    style={{ width: `${autoTotal > 0 ? (autoCycle / autoTotal) * 100 : 0}%` }}
                  />
                </div>
              </div>
            )}

            <p className="text-[11px] text-neutral-400 dark:text-emerald-200/40 mt-2 normal-case tracking-normal">
              <b className="text-neutral-500 dark:text-emerald-200/70">Runs</b> = simultâneas por ciclo (campo acima). <b className="text-neutral-500 dark:text-emerald-200/70">Ciclos</b> = quantas vezes repete sequencialmente. Cada ciclo só inicia após o anterior fechar tudo (runs + navegadores).
            </p>
          </div>

          <CnpjGenerator onAdded={refresh} />

          <div className="mt-4 border-t border-neutral-100 dark:border-neutral-800 pt-3 space-y-1.5">
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              Configure AdsPower e um domínio Netlify em <Link to="/settings" className="underline font-medium">Credenciais</Link> antes — sem isso as runs falham no boot.
            </p>
            <p className="text-xs text-neutral-400">Resolva captchas/2FA nas janelas do AdsPower conforme abrirem.</p>
          </div>
        </Card>
      </div>
    </div>
  )
}

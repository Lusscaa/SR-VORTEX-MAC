import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronRight, Search, Terminal, Fingerprint, Recycle, Hand, Play, Pause,
  Building2, RotateCcw, PencilLine, Power, Download, Undo2,
} from 'lucide-react'
import { api } from '../api.js'
import { usePolling } from '../hooks.js'
import { fmtDuration, timeAgo } from '../utils.js'
import { Badge, Card, CopyButton } from './ui.jsx'

// CNPJ 14 dígitos → 00.000.000/0000-00 (nunca cortar/quebrar feio).
function formatCnpj(v) {
  const d = String(v || '').replace(/\D/g, '')
  if (d.length !== 14) return v || '—'
  return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`
}

// Chip de informação — valor inteiro numa linha (sem corte), quebra pro próximo
// bloco quando não cabe. Substitui o grid apertado que cortava CNPJ/Conta.
function InfoChip({ label, value, mono, children }) {
  if (!value && !children) return null
  return (
    <div className="rounded-lg border border-neutral-200 dark:border-emerald-400/25 bg-white/60 dark:bg-[#07160f]/60 px-3 py-1.5">
      <p className="text-[9px] font-bold uppercase tracking-[0.18em] text-neutral-500 dark:text-emerald-300/60">{label}</p>
      {children || <p className={`text-[15px] font-bold whitespace-nowrap text-neutral-900 dark:text-emerald-50 ${mono ? 'font-mono' : ''}`}>{value}</p>}
    </div>
  )
}

// Botão de ação com ícone + rótulo claro + micro-descrição (o operador entende
// a função de bater o olho). Cores mantêm a semântica antiga de cada ação.
const ABTN = {
  emerald: 'border-emerald-300 text-emerald-700 hover:bg-emerald-50 dark:border-emerald-400/40 dark:text-emerald-300 dark:hover:bg-emerald-400/10',
  sky: 'border-sky-300 text-sky-700 hover:bg-sky-50 dark:border-sky-400/40 dark:text-sky-300 dark:hover:bg-sky-400/10',
  teal: 'border-teal-300 text-teal-700 hover:bg-teal-50 dark:border-teal-400/40 dark:text-teal-300 dark:hover:bg-teal-400/10',
  violet: 'border-violet-300 text-violet-700 hover:bg-violet-50 dark:border-violet-400/40 dark:text-violet-300 dark:hover:bg-violet-400/10',
  amber: 'border-amber-300 text-amber-700 hover:bg-amber-50 dark:border-amber-400/40 dark:text-amber-300 dark:hover:bg-amber-400/10',
  blue: 'border-blue-300 text-blue-700 hover:bg-blue-50 dark:border-blue-400/40 dark:text-blue-300 dark:hover:bg-blue-400/10',
  red: 'border-rose-300 text-rose-700 hover:bg-rose-50 dark:border-rose-400/40 dark:text-rose-300 dark:hover:bg-rose-400/10',
}
function ActionBtn({ tone = 'emerald', icon: Icon, label, desc, busy, busyLabel, onClick, disabled, title }) {
  return (
    <button
      type="button" onClick={onClick} disabled={disabled} title={title}
      className={`group flex items-center gap-2.5 rounded-lg border-2 px-3.5 py-2.5 text-left transition disabled:opacity-40 disabled:cursor-not-allowed ${ABTN[tone]}`}
    >
      <Icon size={20} className="shrink-0" strokeWidth={2.5} />
      <span className="leading-tight">
        <span className="block text-[13px] font-black uppercase tracking-wide">{busy ? busyLabel : label}</span>
        <span className="block text-[10px] font-semibold opacity-80 normal-case tracking-normal">{desc}</span>
      </span>
    </button>
  )
}

const TONE = {
  success: 'green', incomplete: 'amber', failed: 'danger',
  running: 'running', queued: 'neutral', cancelled: 'neutral', assumed: 'amber', paused: 'sky',
}
const LABEL = {
  success: '100% concluído', incomplete: 'incompleto', failed: 'falhou',
  running: 'rodando', queued: 'na fila', cancelled: 'encerrado', assumed: 'assumido', paused: 'pausada',
}
const SHORT = {
  success: '100%', incomplete: 'incompleto', failed: 'falhou',
  running: 'rodando', queued: 'fila', cancelled: 'encerrado', assumed: 'assumido', paused: 'pausada',
}

// Extrai motivo de falha a partir do erro e etapa (mesma lógica do backend)
function getFailureReason(error, step) {
  const e = (error || '').toLowerCase()
  const s = (step || '').toLowerCase()

  // Mensagens de erro explícitas
  if (e.includes('login') || e.includes('senha')) return 'Senha incorreta'
  if (e.includes('selfie') || e.includes('identidade')) return 'Selfie / identidade'
  if (e.includes('restrita') || e.includes('restricted')) return 'Conta restrita'
  if (e.includes('suspensa') || e.includes('suspended')) return 'Conta suspensa'
  if (e.includes('captcha')) return 'Captcha'
  if (e.includes('2fa') || e.includes('two.factor')) return '2FA'
  if (e.includes('netlify') || e.includes('cloudflare') || e.includes('domínio') || e.includes('dominio') || e.includes('nameserver')) return 'Domínio/Netlify'
  if (e.includes('adspower') || e.includes('cdp')) return 'AdsPower'
  if (e.includes('timeout') || e.includes('playwright')) return 'Timeout'

  // Etapa como fallback
  if (s.includes('auth') || s.includes('login')) return 'Login'
  if (s.includes('preflight')) return 'Pré-flight'
  if (s.includes('portfolio')) return 'Portfólio'
  if (s.includes('company') || s.includes('empresa')) return 'Informações'
  if (s.includes('domain')) return 'Domínio'
  if (s.includes('whatsapp') || s.includes('waba')) return 'WhatsApp'
  if (s.includes('verification') || s.includes('verificação')) return 'Verificação'

  return 'Erro desconhecido'
}

// Chips de filtro por status. 'open' = o que normalmente precisa de atenção.
const FILTERS = [
  { key: 'all', label: 'Todos' },
  { key: 'failed', label: 'Falhas' },
  { key: 'incomplete', label: 'Incompletos' },
  { key: 'running', label: 'Rodando' },
  { key: 'success', label: '100%' },
]

function Field({ label, value, mono }) {
  if (!value) return null
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wide text-neutral-400">{label}</p>
      <p className={`text-sm ${mono ? 'font-mono' : ''} break-all`}>{value}</p>
    </div>
  )
}

/**
 * Lista de runs + log ao vivo (atualiza sozinho). Reutilizado na rota /runs
 * (variant="page", master/detail 3 colunas, com filtro+busca) e ao lado do
 * formulário Manual (variant="side", empilhado e compacto). `focusRunId`:
 * quando muda, seleciona aquele run (ex.: a run recém-iniciada no Manual).
 */
export default function RunsPanel({ variant = 'page', focusRunId = null }) {
  const side = variant === 'side'
  const { data: runsData } = usePolling(() => api.runs(), 5000)
  const runs = useMemo(() => runsData || [], [runsData])
  const [selId, setSelId] = useState(null)
  const [log, setLog] = useState('')

  // Filtro por status + busca por CNPJ (só na rota cheia; o painel lateral é
  // sempre da run em foco, não precisa filtrar).
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')

  // Contagem por status p/ mostrar no chip ("Falhas 3").
  const counts = useMemo(() => {
    const c = {}
    for (const r of runs) c[r.status] = (c[r.status] || 0) + 1
    return c
  }, [runs])

  const shown = useMemo(() => {
    let list = runs
    if (!side && filter !== 'all') list = list.filter((r) => r.status === filter)
    const q = query.trim().replace(/\D/g, '')
    if (!side && q) list = list.filter((r) => (r.cnpj || '').replace(/\D/g, '').includes(q))
    return list
  }, [runs, filter, query, side])

  // Auto-seleciona o run mais recente assim que a lista chega.
  useEffect(() => {
    if (!selId && runs.length) setSelId(runs[0].run_id)
  }, [runs, selId])

  // Foco externo (ex.: run iniciada no Manual) → abre esse run.
  useEffect(() => {
    if (focusRunId) setSelId(focusRunId)
  }, [focusRunId])

  // Log ao vivo da run selecionada.
  useEffect(() => {
    if (!selId) return
    let alive = true
    const load = () => api.runLog(selId).then((r) => alive && setLog(r.log || '(sem log ainda)')).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [selId])

  const sel = useMemo(() => runs.find((r) => r.run_id === selId) || null, [runs, selId])

  // Reiniciar: solta conta+CNPJ presos numa run travada e enfileira uma run
  // nova com o mesmo par. Não some da lista — vira 'cancelled'; a run nova
  // aparece como item separado (focusRunId não muda aqui de propósito, o
  // operador acompanha pela lista mesmo).
  const [retrying, setRetrying] = useState(false)
  const [retryMsg, setRetryMsg] = useState(null)
  // busyAction: qual ação está rodando ('retry'|'terminate'|'assume'|null) —
  // desabilita todos os botões e mostra o "…" no botão certo.
  const [busyAction, setBusyAction] = useState(null)
  const retry = async () => {
    if (!sel) return
    setBusyAction('retry'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.retryRun(sel.run_id)
      setRetryMsg({ ok: true, text: `Run nova: ${res.new_run_id}.${res.warning ? ` ⚠ ${res.warning}` : ''}` })
      setSelId(res.new_run_id)
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Continuar (mesmo CNPJ): enfileira uma run NOVA com a MESMA conta + MESMO
  // CNPJ (sem descartar). O pipeline reaproveita perfil/BM/domínio já feitos e
  // segue de onde parou. Diferente do 'Reiniciar', que puxa um CNPJ novo.
  const rerunSame = async () => {
    if (!sel) return
    if (!window.confirm('Continuar com o MESMO CNPJ?\n\nEnfileira uma run nova com a mesma conta e o mesmo CNPJ. O bot reaproveita a BM já criada e o domínio já verificado e segue de onde parou (ex.: WhatsApp). Use quando o Meta ainda estava verificando o domínio.')) return
    setBusyAction('rerun-same'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.rerunSame(sel.run_id)
      setRetryMsg({ ok: true, text: `Run nova (mesmo CNPJ): ${res.new_run_id}.${res.warning ? ` ⚠ ${res.warning}` : ''}` })
      setSelId(res.new_run_id)
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Continuar BM: mesma conta + mesmo CNPJ + MESMA BM, limpando as flags dos
  // passos que ainda não entregaram ativo (hoje: conta de anúncios). Preserva
  // o que já está pronto — portfólio, dados da empresa e domínio verificado.
  const continuarBm = async () => {
    if (!sel) return
    if (!window.confirm('Continuar esta BM?\n\nRefaz só o que falta: CONTA DE ANÚNCIOS e VERIFICAÇÃO da empresa.\n\nMesma conta do Facebook, mesmo CNPJ e mesma BM — nenhum CNPJ novo é gasto. O portfólio, os dados da empresa e o domínio já verificado são preservados.')) return
    setBusyAction('continuar-bm'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.continuarBm(sel.run_id)
      const limpas = (res.flags_limpas || []).length ? ` Refazendo: ${res.flags_limpas.join(', ')}.` : ''
      const mant = (res.preservado || []).length ? ` Preservado: ${res.preservado.join(', ')}.` : ''
      setRetryMsg({ ok: true, text: `Run nova (mesma BM): ${res.new_run_id}.${limpas}${mant}` })
      setSelId(res.new_run_id)
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Encerrar: para o bot, DESCARTA conta+CNPJ (saem do pool) e fecha o perfil.
  const terminate = async () => {
    if (!sel) return
    if (!window.confirm('Encerrar esta run?\n\nO bot para, a CONTA e o CNPJ são DESCARTADOS (saem do pool, não puxam mais) e o perfil AdsPower é fechado. Não dá pra desfazer.')) return
    setBusyAction('terminate'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.terminateRun(sel.run_id)
      setRetryMsg({ ok: true, text: `Run encerrada — conta descartada: ${res.account_discarded ? 'sim' : 'não'}, CNPJ descartado: ${res.cnpj_discarded ? 'sim' : 'não'}, perfil AdsPower excluído: ${res.profile_deleted ? 'sim' : 'não'}.${res.warning ? ` ⚠ ${res.warning}` : ''}` })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Voltar Conta: para o bot, LIBERA a conta ao pool (sem descartar), descarta o CNPJ e fecha o perfil.
  // Usado quando o erro é de IP/reputação (não é da conta).
  const releaseAccount = async () => {
    if (!sel) return
    if (!window.confirm('Voltar conta ao pool?\n\nO bot para, a CONTA volta ao pool (reutilizável), o CNPJ é DESCARTADO e o perfil AdsPower é fechado.')) return
    setBusyAction('release'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.releaseAccountRun(sel.run_id)
      setRetryMsg({ ok: true, text: `Conta devolvida ao pool — ${res.message} CNPJ descartado: ${res.cnpj_discarded ? 'sim' : 'não'}, perfil AdsPower excluído: ${res.profile_deleted ? 'sim' : 'não'}.${res.warning ? ` ⚠ ${res.warning}` : ''}` })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Recuperar Conta + CNPJ: devolve AMBOS ao pool (conta available + CNPJ
  // available). Usar quando a run falhou SEM criar BM (ex.: AdsPower fechado):
  // nada foi usado na Meta, então nem conta nem CNPJ devem ser perdidos.
  const recoverAccountCnpj = async () => {
    if (!sel) return
    if (!window.confirm('Recuperar CONTA + CNPJ para o pool?\n\nDevolve a conta E o CNPJ ao estoque (ambos ficam disponíveis pra novos runs). Use quando a run falhou antes de criar a BM (AdsPower fechado, proxy, pré-flight) — nada foi gasto na Meta.')) return
    setBusyAction('recover'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.recoverAccountCnpj(sel.run_id)
      setRetryMsg({ ok: true, text: `Conta ${res.account_released ? 'devolvida' : 'não devolvida'} + CNPJ ${res.cnpj_restored ? 'devolvido' : 'não devolvido'} ao pool. Perfil AdsPower ${res.profile_deleted ? 'fechado' : 'não fechado'}.` })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Resgatar Conta: resgata uma conta que foi descartada (status=discarded) de volta ao pool.
  // Quando uma run falha e a conta é descartada, o operador pode usar isso pra trazer ela de volta.
  const rescueAccount = async () => {
    if (!sel) return
    if (!window.confirm(`Resgatar conta "${sel.label}" para o pool?\n\nA conta será ativada novamente e poderá ser usada em novos runs.`)) return
    setBusyAction('rescue'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.rescueAccount(sel.label)
      setRetryMsg({ ok: true, text: `✓ Conta resgatada com sucesso! "${sel.label}" voltou ao pool como 'available' — pode ser usada em novos runs.` })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Descartar Conta: marca uma conta como POOL_DISCARDED (removida do pool permanentemente).
  // Operador pode usar isso pra limpar contas problemáticas, bloqueadas, etc.
  const discardAccountManual = async () => {
    if (!sel) return
    if (!window.confirm(`DESCARTAR conta "${sel.label}" PERMANENTEMENTE?\n\n⚠️  Esta ação é IRREVERSÍVEL.\nA conta será marcada como 'discarded' e NÃO poderá mais ser usada.`)) return
    setBusyAction('discard'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.discardAccount(sel.label)
      setRetryMsg({ ok: true, text: `✓ Conta descartada! "${sel.label}" foi removida do pool — não será mais usada em nenhum run.` })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Reiniciar para preencher empresa: volta ao passo de INFORMAÇÕES DA EMPRESA
  // MANTÉM navegador aberto (mesma sessão, cookies, cache).
  const restartToBusinessDetails = async () => {
    if (!sel) return
    if (!window.confirm('Reiniciar no passo de PREENCHER INFORMAÇÕES DA EMPRESA?\n\nO navegador continua aberto — mesma sessão, cookies, cache.\nTodos os passos posteriores (domínio, portfolio, etc) serão limpos.')) return

    setBusyAction('restart-business'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.restartToBusinessDetails(sel.run_id)
      const msg = `✅ REINICIANDO NO PASSO DE EMPRESA!\n\n📊 Voltou de: ${res.previous_step || '?'}\n➡️  Retomando de: ${res.resuming_from_step || '?'}\n\n🌐 Navegador: ABERTO (mesma sessão, cookies, cache)\n⏱️  Agora você pode preencher os dados da empresa novamente...\n\n${res.message}`
      alert(msg)
      setRetryMsg({ ok: true, text: `✓ Reiniciado! Voltou para PREENCHER EMPRESA. Navegador aberto — mesma sessão.` })
      // Atualiza a run pra mostrar o novo status
      setTimeout(() => refresh?.(), 2000)
    } catch (e) {
      setRetryMsg({ ok: false, text: `❌ Erro ao reiniciar: ${e.message}` })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Pausar: o bot entra em espera no próximo checkpoint, sem cancelar nada.
  const pauseRun = async () => {
    if (!sel) return
    setBusyAction('pause'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.pauseRun(sel.run_id)
      setRetryMsg({ ok: true, text: res.message })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Retomar: continua a run pausada de onde parou.
  const resumePaused = async () => {
    if (!sel) return
    setBusyAction('resume'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.resumeRun(sel.run_id)
      setRetryMsg({ ok: true, text: res.message })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // Assumir: para o bot mas MANTÉM o perfil aberto p/ conclusão manual.
  const assume = async () => {
    if (!sel) return
    setBusyAction('assume'); setRetrying(true); setRetryMsg(null)
    try {
      const res = await api.assumeRun(sel.run_id)
      setRetryMsg({ ok: true, text: `Run assumida — bot parado na etapa '${res.stopped_at_step}'. Perfil AdsPower mantido aberto para você concluir manualmente.` })
    } catch (e) {
      setRetryMsg({ ok: false, text: e.message })
    } finally {
      setRetrying(false); setBusyAction(null)
    }
  }

  // --- Log: recolhido por padrão (é saída técnica/dev — não assusta de cara),
  // com "seguir" (auto-scroll p/ o fim) + busca por linha quando aberto ---
  const preRef = useRef(null)
  const followRef = useRef(true)
  const [follow, setFollow] = useState(true)
  const [logQuery, setLogQuery] = useState('')
  const [showLog, setShowLog] = useState(true)

  // Ao trocar de run, mantém o log ABERTO (operador pediu sempre visível),
  // volta a seguir o fim e limpa a busca.
  useEffect(() => { followRef.current = true; setFollow(true); setLogQuery(''); setShowLog(true); setRetryMsg(null) }, [selId])

  const onLogScroll = () => {
    const el = preRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    followRef.current = atBottom
    setFollow(atBottom)
  }

  const shownLog = useMemo(() => {
    if (!logQuery.trim()) return log
    const q = logQuery.toLowerCase()
    const hits = log.split('\n').filter((l) => l.toLowerCase().includes(q))
    return hits.length ? hits.join('\n') : '(nenhuma linha corresponde)'
  }, [log, logQuery])

  // Cola no fim quando está seguindo (e não filtrando). Preserva a posição
  // quando o operador subiu pra ler algo — o poll não o joga mais pro topo.
  useEffect(() => {
    if (showLog && followRef.current && !logQuery && preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight
    }
  }, [shownLog, logQuery, showLog])

  const download = () => {
    const blob = new Blob([log], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `run_${selId}.log`
    a.click()
  }

  const list = (
    <Card className={side ? 'max-h-72 overflow-y-auto p-2' : 'lg:col-span-1 max-h-[78vh] overflow-y-auto p-2'}>
      {!side && (
        <div className="sticky top-0 z-10 -mx-2 -mt-2 mb-1 bg-white/95 dark:bg-neutral-900/95 backdrop-blur px-2 pt-2 pb-1.5 border-b border-neutral-100 dark:border-neutral-800">
          <div className="relative mb-1.5">
            <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-neutral-400" />
            <input
              value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Buscar CNPJ…" inputMode="numeric"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-800 bg-transparent pl-8 pr-3 py-1.5 text-sm outline-none focus:border-neutral-400 dark:focus:border-neutral-600"
            />
          </div>
          <div className="flex flex-wrap gap-1">
            {FILTERS.map((f) => {
              const n = f.key === 'all' ? runs.length : (counts[f.key] || 0)
              const active = filter === f.key
              return (
                <button
                  key={f.key} type="button" onClick={() => setFilter(f.key)}
                  className={`rounded-full px-2 py-0.5 text-[11px] font-medium transition ${
                    active
                      ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                      : 'bg-neutral-100 text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400 hover:bg-neutral-200 dark:hover:bg-neutral-700'
                  }`}
                >
                  {f.label} {n > 0 && <span className="tabular-nums opacity-70">{n}</span>}
                </button>
              )
            })}
          </div>
        </div>
      )}
      {shown.length === 0 && (
        <p className="text-sm text-neutral-400 p-3">
          {runs.length === 0 ? 'Nenhum run ainda.' : 'Nenhum run com esse filtro.'}
        </p>
      )}
      {shown.map((r) => (
        <button
          key={r.run_id} onClick={() => setSelId(r.run_id)}
          className={`block w-full text-left rounded-lg border px-3 py-2.5 mb-1.5 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60 ${
            selId === r.run_id
              ? 'bg-neutral-900 text-white border-neutral-900 dark:bg-white dark:text-neutral-900 dark:border-white dark:shadow-[0_0_20px_-6px_rgba(52,211,153,0.5)]'
              : 'border-neutral-200 hover:bg-neutral-100 dark:border-emerald-400/20 dark:hover:bg-emerald-400/5 dark:hover:border-emerald-400/45'
          }`}
        >
          <div className="flex items-center justify-between gap-2">
            <span className={`font-display font-bold text-[15px] tracking-wide truncate ${selId === r.run_id ? '' : 'dark:text-emerald-50'}`}>{r.cnpj ? formatCnpj(r.cnpj) : '—'}</span>
            <Badge tone={selId === r.run_id ? 'neutral' : (TONE[r.status] || 'neutral')}>
              {r.status === 'failed' ? getFailureReason(r.error, r.step) : (SHORT[r.status] || r.status)}
            </Badge>
          </div>
          <p className={`text-xs font-semibold truncate mt-0.5 ${selId === r.run_id ? 'opacity-70' : 'text-neutral-500 dark:text-emerald-200/70'}`}>{r.label || '—'}</p>
          <div className="flex items-center justify-between gap-2 mt-0.5">
            <span className="text-[11px] truncate flex items-center gap-1">
              {(r.profile_serial_number || r.profile_user_id) && (
                <>
                  <span className="font-bold bg-blue-500 text-white px-2 py-0.5 rounded text-[10px] shrink-0">
                    perfil {r.profile_serial_number || r.profile_user_id}
                  </span>
                  {r.step && <span className="opacity-50">·</span>}
                </>
              )}
              {r.step && <span className="opacity-50">etapa: {r.step}</span>}
            </span>
            <span className="text-[11px] opacity-50 tabular-nums shrink-0">
              {r.status === 'running' && r.started_at
                ? fmtDuration(r.started_at)
                : timeAgo(r.created_at)}
            </span>
          </div>
        </button>
      ))}
    </Card>
  )

  const detail = (
    <Card className={side ? '' : 'lg:col-span-2'}>
      {!sel ? (
        <p className="text-sm text-neutral-400 py-10 text-center">
          {side ? 'Inicie um run ao lado para ver o log aqui.' : 'Selecione um run à esquerda.'}
        </p>
      ) : (
        <>
          {/* CABEÇALHO: CNPJ herói (formatado, nunca cortado) + status + chips */}
          <div className="mb-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-3 min-w-0">
                <span className="cyber-ibadge gold h-11 w-11 shrink-0"><Fingerprint size={20} /></span>
                <div className="min-w-0">
                  <p className="hud-tag hidden dark:block">// CNPJ</p>
                  <p className="text-[10px] font-bold uppercase tracking-wider text-neutral-400 dark:hidden">CNPJ</p>
                  <p className="font-display text-xl sm:text-2xl font-black tracking-wide tabular-nums text-neutral-900 dark:text-emerald-50 dark:[text-shadow:0_0_18px_rgba(52,211,153,0.35)]">{formatCnpj(sel.cnpj)}</p>
                </div>
              </div>
              <Badge tone={TONE[sel.status] || 'neutral'}>
                {sel.status === 'failed' ? getFailureReason(sel.error, sel.step) : (LABEL[sel.status] || sel.status)}
              </Badge>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <InfoChip label="Conta" value={sel.label} mono />
              <InfoChip label="Perfil AdsPower">
                <span className="inline-block font-bold bg-blue-500 text-white px-2 py-0.5 rounded text-sm font-mono">{sel.profile_serial_number || sel.profile_user_id || '—'}</span>
              </InfoChip>
              <InfoChip label="Etapa" value={sel.step} />
              <InfoChip label="Iniciado" value={sel.started_at ? timeAgo(sel.started_at) : (sel.created_at ? `fila ${timeAgo(sel.created_at)}` : null)} />
              <InfoChip label="Duração" value={sel.started_at ? fmtDuration(sel.started_at, sel.finished_at) : null} />
              <InfoChip label="BM ID" value={sel.business_id} mono />
              <InfoChip label="WABA" value={sel.waba_id} mono />
              <InfoChip label="BM Invite" value={sel.bm_invite_sent ? `✓ ${sel.bm_invite_email || 'enviado'}` : null} />
            </div>
          </div>

          {/* AÇÕES: barra clara e agrupada — cada botão diz o que faz */}
          {sel.status !== 'success' && (
            <div className="mb-4">
              <p className="hud-tag hidden dark:block mb-1.5">// AÇÕES DISPONÍVEIS</p>
              <div className="flex flex-wrap gap-2">
                <ActionBtn tone="emerald" icon={Recycle} label="Recuperar Conta" desc="devolve a conta (CNPJ descartado)"
                  busy={busyAction === 'release'} busyLabel="Recuperando…" onClick={releaseAccount} disabled={retrying}
                  title="Devolve a CONTA ao pool. O CNPJ é descartado (use quando o CNPJ já foi usado na Meta)." />
                <ActionBtn tone="teal" icon={Undo2} label="Recuperar Conta + CNPJ" desc="devolve os DOIS ao pool"
                  busy={busyAction === 'recover'} busyLabel="Recuperando…" onClick={recoverAccountCnpj} disabled={retrying}
                  title="Devolve CONTA e CNPJ ao pool. Use quando a run falhou ANTES de criar a BM (AdsPower fechado, proxy, pré-flight) — nada foi gasto na Meta." />
                {sel.status === 'paused' && (
                  <ActionBtn tone="emerald" icon={Play} label="Retomar" desc="continua de onde parou"
                    busy={busyAction === 'resume'} busyLabel="Retomando…" onClick={resumePaused} disabled={retrying}
                    title="Retoma a run de onde parou — o bot continua o mesmo pipeline, mesma task, sem criar run nova." />
                )}
                {sel.status !== 'cancelled' && sel.status !== 'assumed' && sel.status !== 'paused' && (
                  <>
                    {sel.status === 'running' && (
                      <ActionBtn tone="sky" icon={Pause} label="Pausar" desc="espera no próximo checkpoint"
                        busy={busyAction === 'pause'} busyLabel="Pausando…" onClick={pauseRun} disabled={retrying}
                        title="Pausa temporariamente — o bot espera no próximo checkpoint seguro (browser e perfil ficam abertos). Use 'Retomar' depois." />
                    )}
                    <ActionBtn tone="sky" icon={Hand} label="Assumir" desc="para o bot, perfil fica aberto"
                      busy={busyAction === 'assume'} busyLabel="Assumindo…" onClick={assume} disabled={retrying}
                      title="Para o bot e MANTÉM o perfil AdsPower aberto p/ você concluir manualmente. Mostra em que etapa parou." />
                    <ActionBtn tone="teal" icon={Play} label="Continuar (mesmo CNPJ)" desc="reaproveita BM/domínio e segue"
                      busy={busyAction === 'rerun-same'} busyLabel="Continuando…" onClick={rerunSame} disabled={retrying}
                      title="Continua com a MESMA conta e o MESMO CNPJ: reaproveita BM e domínio já feitos e segue de onde parou. Use quando o Meta ainda estava verificando o domínio." />
                    <ActionBtn tone="violet" icon={Building2} label="Continuar BM" desc="refaz anúncios + verificação"
                      busy={busyAction === 'continuar-bm'} busyLabel="Continuando BM…" onClick={continuarBm} disabled={retrying}
                      title="Refaz só o que falta na MESMA BM: conta de anúncios + verificação. Mesma conta, mesmo CNPJ, mesmo portfólio — preserva dados da empresa e domínio já verificado." />
                    <ActionBtn tone="amber" icon={RotateCcw} label="Reiniciar" desc="novo run com o mesmo par"
                      busy={busyAction === 'retry'} busyLabel="Reiniciando…" onClick={retry} disabled={retrying}
                      title="Solta a conta+CNPJ presos a esta run e enfileira uma run nova com o mesmo par" />
                    <ActionBtn tone="blue" icon={PencilLine} label="Preencher Empresa" desc="volta ao passo, navegador aberto"
                      busy={busyAction === 'restart-business'} busyLabel="Reiniciando…" onClick={restartToBusinessDetails} disabled={retrying}
                      title="Volta ao passo de PREENCHER INFORMAÇÕES DA EMPRESA. MANTÉM navegador aberto (mesma sessão, cookies, cache)." />
                    <ActionBtn tone="red" icon={Power} label="Encerrar" desc="descarta conta + CNPJ"
                      busy={busyAction === 'terminate'} busyLabel="Encerrando…" onClick={terminate} disabled={retrying}
                      title="Encerra a run de vez: descarta conta+CNPJ (saem do pool) e fecha o perfil. Não puxa mais." />
                  </>
                )}
              </div>
            </div>
          )}
          {retryMsg && (
            <p className={`mb-2 rounded-lg border px-3 py-2 text-xs ${retryMsg.ok ? 'border-lime-300 dark:border-lime-800 bg-lime-50 dark:bg-lime-950/30 text-lime-700 dark:text-lime-400' : 'border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 text-red-600 dark:text-red-400'}`}>{retryMsg.text}</p>
          )}
          {/* Mensagem humana de erro — sempre visível (não é saída técnica). */}
          {sel.error && (
            <p className="mb-2 rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-xs text-red-600 dark:text-red-400">{sel.error}</p>
          )}
          {/* Log ao vivo — SEMPRE visível (operador pediu). Terminal cyber. */}
          <div className="mt-1">
            <div className="flex items-center justify-between gap-2 mb-2">
              <div className="flex items-center gap-2">
                <span className="cyber-ibadge green h-7 w-7"><Terminal size={14} /></span>
                <span className="font-display text-xs font-bold uppercase tracking-wider text-neutral-700 dark:text-emerald-200">Log ao vivo</span>
                <button type="button" onClick={() => setShowLog((v) => !v)}
                  className="inline-flex items-center gap-0.5 text-[11px] text-neutral-400 hover:text-neutral-700 dark:hover:text-emerald-200 transition">
                  <ChevronRight size={12} className={`transition-transform ${showLog ? 'rotate-90' : ''}`} />
                  {showLog ? 'ocultar' : 'mostrar'}
                </button>
              </div>
              {showLog && (
                <div className="flex items-center gap-1.5">
                  <CopyButton text={log} label="Copiar" className="rounded-lg border border-neutral-300 dark:border-emerald-400/30 text-neutral-600 dark:text-emerald-200 px-2.5 py-1 text-[11px] font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10" />
                  <button type="button" onClick={download} className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/30 text-neutral-600 dark:text-emerald-200 px-2.5 py-1 text-[11px] font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10"><Download size={12} /> Baixar</button>
                </div>
              )}
            </div>
            {showLog && (
              <>
                <div className="flex items-center justify-between gap-2 mb-1.5">
                  <div className="relative flex-1 max-w-xs">
                    <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-neutral-400" />
                    <input
                      value={logQuery} onChange={(e) => setLogQuery(e.target.value)}
                      placeholder="Filtrar linhas do log…"
                      className="w-full rounded-lg border border-neutral-200 dark:border-emerald-400/20 bg-transparent dark:bg-[#07130d]/50 pl-7 pr-2 py-1 text-xs outline-none focus:border-neutral-400 dark:focus:border-emerald-400/50"
                    />
                  </div>
                  <span className="text-[11px] text-neutral-400 dark:text-emerald-300/50 shrink-0">
                    {logQuery ? 'busca ativa' : follow ? 'seguindo o fim' : 'role até o fim p/ seguir'}
                  </span>
                </div>
                <pre
                  ref={preRef} onScroll={onLogScroll}
                  style={{ color: '#c8ffe6', textShadow: '0 0 10px rgba(52,211,153,0.25)' }}
                  className={`overflow-auto rounded-xl border border-emerald-400/30 bg-[#03130c] p-4 text-[13px] leading-[1.65] font-medium whitespace-pre-wrap font-mono shadow-[inset_0_0_30px_-14px_rgba(52,211,153,0.7)] ${side ? 'max-h-[52vh]' : 'max-h-[58vh]'}`}
                >{shownLog || '(sem log ainda)'}</pre>
              </>
            )}
          </div>
        </>
      )}
    </Card>
  )

  if (side) {
    return <div className="grid gap-3 min-w-0">{list}{detail}</div>
  }
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      {list}
      {detail}
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import {
  Building2, MessageSquare, Send, Users, FileText, Plus, Trash2, Link2,
  Phone, CheckCircle2, XCircle, RefreshCw, Search, Tag as TagIcon, X, Rocket,
  KeyRound, ChevronRight, Loader2,
} from 'lucide-react'
import { api } from '../api.js'
import {
  Card, PageHead, Stat, Badge, btnPrimary, btnGhost, inputCls, CopyButton, Skeleton,
} from '../components/ui.jsx'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const TABS = [
  { key: 'bms', label: 'Conectar BMs', icon: Building2 },
  { key: 'templates', label: 'Templates', icon: FileText },
  { key: 'disparar', label: 'Disparar', icon: Rocket },
  { key: 'leads', label: 'Leads', icon: Users },
  { key: 'chat', label: 'Chat', icon: MessageSquare },
]

const VAR_RE = /\{\{\s*(\d+)\s*\}\}/g

function bodyOf(tpl) {
  const c = (tpl?.components || []).find((x) => (x.type || '').toUpperCase() === 'BODY')
  return c?.text || ''
}
function headerTextOf(tpl) {
  const c = (tpl?.components || []).find(
    (x) => (x.type || '').toUpperCase() === 'HEADER' && (x.format || 'TEXT').toUpperCase() === 'TEXT'
  )
  return c?.text || ''
}
function varsOf(text) {
  const nums = new Set()
  let m
  const re = new RegExp(VAR_RE)
  while ((m = re.exec(text || ''))) nums.add(Number(m[1]))
  return [...nums].sort((a, b) => a - b)
}
function tplStatusTone(status) {
  const s = (status || '').toUpperCase()
  if (s === 'APPROVED') return 'green'
  if (['REJECTED', 'DISABLED', 'PAUSED'].includes(s)) return 'danger'
  return 'warn'
}
function tplStatusLabel(status) {
  const map = { APPROVED: 'Aprovado', PENDING: 'Em análise', REJECTED: 'Rejeitado' }
  return map[(status || '').toUpperCase()] || status || '—'
}
function fmtPhone(digits) {
  const d = (digits || '').replace(/\D/g, '')
  if (d.length >= 12) return `+${d.slice(0, 2)} (${d.slice(2, 4)}) ${d.slice(4, 9)}-${d.slice(9)}`
  if (d.length === 11) return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7)}`
  return digits || '—'
}

function Toast({ msg }) {
  if (!msg) return null
  const ok = msg.ok
  return (
    <div className={`rounded-lg border px-3 py-2 text-sm ${ok
      ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
      : 'border-rose-400/40 bg-rose-500/10 text-rose-700 dark:text-rose-300'}`}>
      {msg.text}
    </div>
  )
}

function SectionTitle({ icon: Icon, children, hint }) {
  return (
    <div className="mb-3">
      <h2 className="font-display text-lg font-extrabold uppercase tracking-wide cyber-gold flex items-center gap-2">
        {Icon && <Icon size={18} />} {children}
      </h2>
      {hint && <p className="text-xs text-neutral-500 dark:text-emerald-200/50 mt-0.5">{hint}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sub-aba: Conectar BMs
// ---------------------------------------------------------------------------
// Card de configuração do webhook (respostas dos leads + status de entrega).
function WebhookCard() {
  const [cfg, setCfg] = useState(null)
  const [verify, setVerify] = useState('')
  const [secret, setSecret] = useState('')
  const [pub, setPub] = useState('')
  const [open, setOpen] = useState(false)
  const [msg, setMsg] = useState(null)
  const load = async () => { try { const c = await api.crmWebhookConfig(); setCfg(c); setVerify(c.verify_token || ''); setPub(c.public_url || '') } catch { /* ignore */ } }
  useEffect(() => { load() }, [])
  const save = async () => {
    setMsg(null)
    try { await api.crmSetWebhookConfig({ verify_token: verify, app_secret: secret, public_url: pub }); setSecret(''); setMsg({ ok: true, text: 'Salvo.' }); load() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  const fullUrl = pub ? `${pub.replace(/\/+$/, '')}/api/crm/webhook` : ''
  return (
    <Card className="space-y-2">
      <button className="flex items-center justify-between w-full" onClick={() => setOpen((v) => !v)}>
        <span className="font-display text-sm font-extrabold uppercase tracking-wide cyber-gold flex items-center gap-2">
          <Link2 size={16} /> Webhook (respostas dos leads)
        </span>
        <span className="text-xs text-neutral-400">{cfg?.has_verify_token && pub ? '● configurado' : 'opcional — só p/ RECEBER respostas'} {open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="space-y-2 pt-1">
          <p className="text-[11px] text-neutral-500 dark:text-emerald-200/50">
            <b>Opcional.</b> Só precisa disto para <b>RECEBER</b> as respostas dos leads no Chat. Para DISPARAR não é necessário. Requer uma URL pública HTTPS (ex.: túnel <span className="font-mono">*.trycloudflare.com</span>) apontando pro backend :8000.
          </p>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-neutral-400 w-28 shrink-0">URL pública (túnel)</span>
            <input value={pub} onChange={(e) => setPub(e.target.value)} placeholder="https://xxxx.trycloudflare.com" className={`${inputCls} py-1.5 text-xs font-mono`} />
          </div>
          {fullUrl && (
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-neutral-400 w-28 shrink-0">→ URL p/ a Meta</span>
              <input readOnly value={fullUrl} className={`${inputCls} py-1.5 text-xs font-mono text-emerald-600 dark:text-emerald-300`} />
              <CopyButton text={fullUrl} label="⧉" className="text-xs text-neutral-400 hover:text-neutral-200 shrink-0" />
            </div>
          )}
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-neutral-400 w-28 shrink-0">Verify Token</span>
            <input value={verify} onChange={(e) => setVerify(e.target.value)} placeholder="ex.: vortex_wh_2026" className={`${inputCls} py-1.5 text-sm`} />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-neutral-400 w-28 shrink-0">App Secret</span>
            <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={cfg?.has_app_secret ? '•••••• (salvo)' : 'app secret p/ validar assinatura'} className={`${inputCls} py-1.5 text-sm font-mono`} />
          </div>
          <div className="flex items-center gap-3">
            <button className={`${btnPrimary} px-4 py-1.5 text-sm`} onClick={save}>Salvar webhook</button>
            <Toast msg={msg} />
          </div>
        </div>
      )}
    </Card>
  )
}

// Adiciona uma WABA manualmente (número de teste, ou WABA que o bot não capturou).
function ManualWaba({ reload }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [waba, setWaba] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const add = async () => {
    setBusy(true); setMsg(null)
    try {
      const r = await api.crmAddManualBm({ name, waba_id: waba.trim(), token: token.trim() })
      setMsg({ ok: true, text: `WABA "${r.waba_name || r.business_id}" adicionada!` })
      setName(''); setWaba(''); setToken(''); reload()
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  return (
    <Card className="space-y-2">
      <button className="flex items-center justify-between w-full" onClick={() => setOpen((v) => !v)}>
        <span className="font-display text-sm font-extrabold uppercase tracking-wide cyber-gold flex items-center gap-2">
          <Plus size={16} /> Adicionar WABA manual (número de teste)
        </span>
        <span className="text-xs text-neutral-400">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="space-y-2 pt-1">
          <p className="text-[11px] text-neutral-500 dark:text-emerald-200/50">
            Use pra testar com o <b>número de teste</b> do app, ou qualquer WABA. Pegue o <b>WABA ID</b> + <b>token</b> na Meta (App → WhatsApp → Configuração da API / "Etapa 1. Experimente").
          </p>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-neutral-400 w-24 shrink-0">Nome</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="ex.: Teste Cleuza" className={`${inputCls} py-1.5 text-sm`} />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-neutral-400 w-24 shrink-0">WABA ID</span>
            <input value={waba} onChange={(e) => setWaba(e.target.value)} placeholder="ID da conta do WhatsApp Business" className={`${inputCls} py-1.5 text-sm font-mono`} />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-neutral-400 w-24 shrink-0">Token</span>
            <textarea value={token} onChange={(e) => setToken(e.target.value)} rows={2} placeholder="EAAG… (token do app / temporário)" className={`${inputCls} py-1.5 text-xs font-mono`} />
          </div>
          <div className="flex items-center gap-3">
            <button className={`${btnPrimary} px-4 py-1.5 text-sm inline-flex items-center gap-2`} disabled={busy || !waba.trim() || token.trim().length < 20} onClick={add}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Adicionar
            </button>
            <Toast msg={msg} />
          </div>
        </div>
      )}
    </Card>
  )
}

function BmsTab({ bms, loading, reload }) {
  return (
    <div className="space-y-4">
      <SectionTitle icon={Building2} hint="BMs do bot (com WABA) + WABAs adicionadas manualmente (número de teste).">
        Business Managers
      </SectionTitle>
      <WebhookCard />
      <ManualWaba reload={reload} />
      {loading ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => <Skeleton key={i} className="h-40" />)}
        </div>
      ) : bms.length === 0 ? (
        <Card><p className="text-sm text-neutral-400 py-6 text-center">
          Nenhuma BM com WhatsApp (WABA) ainda. Provisione/finalize a verificação de uma BM com WhatsApp para ela aparecer aqui.
        </p></Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {bms.map((bm) => <BmConnectCard key={bm.business_id} bm={bm} reload={reload} />)}
        </div>
      )}
    </div>
  )
}

// Teste do token da BM — o token temporário da Meta morre em ~24h.
function TokenHealth({ businessId }) {
  const [state, setState] = useState(null)   // null | {ok, dead, error, waba_name}
  const [busy, setBusy] = useState(false)
  const check = async () => {
    setBusy(true)
    try { setState(await api.crmTokenCheck(businessId)) }
    catch (e) { setState({ ok: false, error: e.message }) } finally { setBusy(false) }
  }
  return (
    <div className="flex items-center justify-between text-xs gap-2">
      <span className="text-neutral-400 inline-flex items-center gap-1"><KeyRound size={12} /> Token</span>
      <div className="flex items-center gap-1.5">
        {state && (state.ok
          ? <Badge tone="green">válido</Badge>
          : <Badge tone={state.dead ? 'danger' : 'amber'}>{state.dead ? 'expirado' : 'erro'}</Badge>)}
        <button className={`${btnGhost} px-2 py-0.5 text-[10px]`} disabled={busy} onClick={check}>
          {busy ? '…' : 'testar'}
        </button>
      </div>
    </div>
  )
}

// Assinatura do app aos webhooks da WABA — sem isto a Meta não entrega eventos.
function WebhookSubscribe({ businessId }) {
  const [sub, setSub] = useState(null)      // null=desconhecido, true/false=status
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const check = async () => {
    setBusy(true); setMsg(null)
    try { const r = await api.crmWebhookSubStatus(businessId); setSub(r.subscribed) }
    catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  const subscribe = async () => {
    setBusy(true); setMsg(null)
    try { await api.crmSubscribeWebhook(businessId); setSub(true); setMsg({ ok: true, text: 'App assinado aos webhooks desta BM!' }) }
    catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  return (
    <div>
      <div className="flex items-center justify-between text-xs gap-2">
        <span className="text-neutral-400 inline-flex items-center gap-1"><Link2 size={12} /> Webhook</span>
        <div className="flex items-center gap-1.5">
          {sub === true && <Badge tone="green">assinado</Badge>}
          {sub === false && <Badge tone="amber">não assinado</Badge>}
          {sub !== true && (
            <button className={`${btnGhost} px-2 py-0.5 text-[10px]`} disabled={busy} onClick={sub === null ? check : subscribe}>
              {busy ? '…' : sub === null ? 'checar' : 'assinar'}
            </button>
          )}
        </div>
      </div>
      {msg && !msg.ok && <p className="text-[10px] text-rose-400 mt-0.5">{msg.text}</p>}
    </div>
  )
}

// Ativação do número na Cloud API: status → request_code → verify → register(PIN).
// Só necessário se o número ainda NÃO está CONNECTED (recém-adicionado ao WABA).
function PhoneActivate({ businessId, pnid }) {
  const [status, setStatus] = useState(null)   // objeto do número
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState('idle')     // idle|code|pin
  const [method, setMethod] = useState('SMS')
  const [code, setCode] = useState('')
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)

  const check = async () => {
    setBusy(true); setMsg(null)
    try { const r = await api.crmPhoneStatus(businessId, pnid); setStatus(r.phone); setOpen(true) }
    catch (e) { setMsg({ ok: false, text: e.message }); setOpen(true) } finally { setBusy(false) }
  }
  const reqCode = async () => {
    setBusy(true); setMsg(null)
    try { await api.crmRequestCode(businessId, pnid, { code_method: method, language: 'pt_BR' }); setStep('code'); setMsg({ ok: true, text: `Código enviado por ${method}.` }) }
    catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  const doVerify = async () => {
    setBusy(true); setMsg(null)
    try { await api.crmVerifyCode(businessId, pnid, code.trim()); setStep('pin'); setMsg({ ok: true, text: 'Código confirmado. Defina o PIN de 6 dígitos.' }) }
    catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  const doRegister = async () => {
    setBusy(true); setMsg(null)
    try { await api.crmRegisterPhone(businessId, pnid, pin.trim()); setMsg({ ok: true, text: 'Número ativado (CONNECTED)!' }); setStep('idle'); check() }
    catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }

  const connected = (status?.status || '').toUpperCase() === 'CONNECTED'
  return (
    <div className="mt-2 border-t border-emerald-400/15 pt-2">
      {!open ? (
        <button className={`${btnGhost} w-full py-1.5 text-xs inline-flex items-center justify-center gap-1.5`} disabled={busy} onClick={check}>
          {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Verificar/ativar número
        </button>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-neutral-400">Status Cloud API</span>
            <Badge tone={connected ? 'green' : 'warn'}>{status?.status || '—'}</Badge>
          </div>
          {status && (
            <div className="text-[10px] text-neutral-400 space-y-0.5">
              <p>Qualidade: {status.quality_rating || '—'} · Tier: {status.messaging_limit_tier || '—'}</p>
            </div>
          )}
          {!connected && step === 'idle' && (
            <div className="flex items-center gap-2">
              <select value={method} onChange={(e) => setMethod(e.target.value)} className={`${inputCls} py-1 text-xs max-w-[6rem]`}>
                <option value="SMS">SMS</option><option value="VOICE">Voz</option>
              </select>
              <button className={`${btnPrimary} px-3 py-1.5 text-xs flex-1`} disabled={busy} onClick={reqCode}>Enviar código</button>
            </div>
          )}
          {step === 'code' && (
            <div className="flex items-center gap-2">
              <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="código recebido" className={`${inputCls} py-1.5 text-xs`} />
              <button className={`${btnPrimary} px-3 py-1.5 text-xs`} disabled={busy} onClick={doVerify}>Verificar</button>
            </div>
          )}
          {step === 'pin' && (
            <div className="flex items-center gap-2">
              <input value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 6))} placeholder="PIN 6 dígitos" className={`${inputCls} py-1.5 text-xs font-mono`} />
              <button className={`${btnPrimary} px-3 py-1.5 text-xs`} disabled={busy || pin.length !== 6} onClick={doRegister}>Registrar</button>
            </div>
          )}
          <Toast msg={msg} />
        </div>
      )}
    </div>
  )
}

function BmConnectCard({ bm, reload }) {
  const [token, setToken] = useState('')
  const [phones, setPhones] = useState(null)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)

  const saveToken = async () => {
    setBusy('token'); setMsg(null)
    try {
      await api.crmSetToken(bm.business_id, token.trim())
      setToken(''); setMsg({ ok: true, text: 'Token salvo.' }); reload()
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy('') }
  }
  const loadPhones = async () => {
    setBusy('phones'); setMsg(null)
    try {
      const r = await api.crmPhones(bm.business_id)
      setPhones(r.phones || [])
      if (!r.phones?.length) setMsg({ ok: false, text: 'Nenhum número neste WABA ainda.' })
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy('') }
  }
  const connect = async (ph) => {
    setBusy('connect'); setMsg(null)
    try {
      await api.crmConnect(bm.business_id, {
        phone_number_id: ph.id,
        display_phone: ph.display_phone_number,
        verified_name: ph.verified_name,
      })
      setPhones(null); setMsg({ ok: true, text: 'Número conectado!' }); reload()
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy('') }
  }
  const disconnect = async () => {
    setBusy('disc'); setMsg(null)
    try { await api.crmDisconnect(bm.business_id); reload() }
    catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy('') }
  }

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-bold truncate">{bm.portfolio_name || 'BM sem nome'}</p>
          <p className="text-[11px] text-neutral-400 font-mono">BM {bm.business_id}</p>
          {bm.cnpj && <p className="text-[11px] text-neutral-400">CNPJ {bm.cnpj}</p>}
        </div>
        {bm.connected
          ? <Badge tone="green">Conectada</Badge>
          : bm.has_token ? <Badge tone="warn">Sem número</Badge> : <Badge tone="amber">Sem token</Badge>}
      </div>

      <div className="flex items-center justify-between text-xs">
        <span className="text-neutral-400 inline-flex items-center gap-1"><KeyRound size={12} /> WABA</span>
        <span className="font-mono flex items-center gap-1">{bm.waba_id}<CopyButton text={bm.waba_id} label="⧉" className="text-[10px] px-1 text-neutral-400 hover:text-neutral-200" /></span>
      </div>

      {bm.has_token && <TokenHealth businessId={bm.business_id} />}
      {bm.has_token && <WebhookSubscribe businessId={bm.business_id} />}

      {!bm.has_token && (
        <div className="space-y-1.5">
          <p className="text-[11px] text-neutral-500 dark:text-emerald-200/50">
            Cole um token de <b>Usuário do Sistema</b> (permanente) com permissões whatsapp_business_messaging + management.
          </p>
          <textarea value={token} onChange={(e) => setToken(e.target.value)} rows={2}
            placeholder="EAAG..." className={`${inputCls} font-mono text-xs`} />
          <button className={`${btnPrimary} w-full py-2 text-sm`} disabled={busy === 'token' || token.trim().length < 20} onClick={saveToken}>
            {busy === 'token' ? 'Salvando…' : 'Salvar token'}
          </button>
        </div>
      )}

      {bm.connected && (
        <div className="rounded-lg border border-emerald-400/25 bg-emerald-500/5 px-3 py-2 text-sm">
          <div className="flex items-center gap-2"><Phone size={14} className="text-emerald-400" />
            <span className="font-semibold">{bm.display_phone || fmtPhone('')}</span></div>
          {bm.verified_name && <p className="text-[11px] text-neutral-400 mt-0.5">{bm.verified_name}</p>}
          <p className="text-[10px] text-neutral-500 font-mono mt-0.5">phone_number_id {bm.phone_number_id}</p>
          <PhoneActivate businessId={bm.business_id} pnid={bm.phone_number_id} />
          <button className={`${btnGhost} mt-2 w-full py-1.5 text-xs`} disabled={busy === 'disc'} onClick={disconnect}>Desconectar número</button>
        </div>
      )}

      {bm.has_token && !bm.connected && (
        <div className="space-y-2">
          {phones === null ? (
            <button className={`${btnGhost} w-full py-2 text-sm inline-flex items-center justify-center gap-2`} disabled={busy === 'phones'} onClick={loadPhones}>
              {busy === 'phones' ? <Loader2 size={14} className="animate-spin" /> : <Link2 size={14} />} Buscar números do WABA
            </button>
          ) : (
            <div className="space-y-1.5">
              {phones.map((ph) => (
                <button key={ph.id} disabled={busy === 'connect'} onClick={() => connect(ph)}
                  className="w-full text-left rounded-lg border border-neutral-300 dark:border-emerald-400/25 px-3 py-2 hover:bg-emerald-400/10 transition">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-sm">{ph.display_phone_number}</span>
                    <ChevronRight size={14} className="text-neutral-400" />
                  </div>
                  <p className="text-[11px] text-neutral-400">{ph.verified_name} · qualidade {ph.quality_rating || '—'}</p>
                </button>
              ))}
              <button className={`${btnGhost} w-full py-1.5 text-xs`} onClick={() => setPhones(null)}>cancelar</button>
            </div>
          )}
        </div>
      )}
      <Toast msg={msg} />
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Sub-aba: Templates (listar + criar/submeter)
// ---------------------------------------------------------------------------
function TemplatesTab({ bms }) {
  const withToken = bms.filter((b) => b.has_token)
  const [selBm, setSelBm] = useState(withToken[0]?.business_id || '')
  const [templates, setTemplates] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)
  const [showCreate, setShowCreate] = useState(false)

  useEffect(() => { if (!selBm && withToken[0]) setSelBm(withToken[0].business_id) }, [withToken, selBm])

  const load = async (bid) => {
    if (!bid) return
    setLoading(true); setErr(null); setTemplates(null)
    try { const r = await api.crmTemplates(bid); setTemplates(r.templates || []) }
    catch (e) { setErr(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load(selBm) }, [selBm])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <SectionTitle icon={FileText} hint="Liste os templates de uma BM e submeta um novo para aprovação em várias BMs de uma vez.">
          Templates de mensagem
        </SectionTitle>
        <button className={`${btnPrimary} px-4 py-2 text-sm inline-flex items-center gap-2`} onClick={() => setShowCreate((v) => !v)}>
          <Plus size={16} /> {showCreate ? 'Fechar' : 'Criar template'}
        </button>
      </div>

      {showCreate && <CreateTemplate bms={withToken} onDone={() => { setShowCreate(false); load(selBm) }} />}

      <Card>
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <label className="text-xs text-neutral-400">BM:</label>
          <select value={selBm} onChange={(e) => setSelBm(e.target.value)} className={`${inputCls} max-w-xs py-1.5 text-sm`}>
            {withToken.length === 0 && <option value="">— nenhuma BM com token —</option>}
            {withToken.map((b) => <option key={b.business_id} value={b.business_id}>{b.portfolio_name || b.business_id}</option>)}
          </select>
          <button className={`${btnGhost} px-3 py-1.5 text-xs inline-flex items-center gap-1`} onClick={() => load(selBm)} disabled={loading}>
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Atualizar
          </button>
        </div>
        {err && <Toast msg={{ ok: false, text: err }} />}
        {loading ? <Skeleton className="h-24 mt-2" /> : (
          <div className="space-y-2">
            {(templates || []).length === 0 && !err && <p className="text-sm text-neutral-400 py-4 text-center">Nenhum template nesta BM.</p>}
            {(templates || []).map((t) => (
              <div key={`${t.name}-${t.language}`} className="rounded-lg border border-neutral-200 dark:border-emerald-400/15 p-3">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-2">
                    <span className="font-bold">{t.name}</span>
                    <Badge tone="neutral">{t.category}</Badge>
                    <span className="text-[11px] text-neutral-400">{t.language}</span>
                  </div>
                  <Badge tone={tplStatusTone(t.status)}>{tplStatusLabel(t.status)}</Badge>
                </div>
                <p className="text-xs text-neutral-500 dark:text-emerald-100/70 mt-1.5 whitespace-pre-wrap">{bodyOf(t)}</p>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function CreateTemplate({ bms, onDone }) {
  const [name, setName] = useState('')
  const [category, setCategory] = useState('MARKETING')
  const [language, setLanguage] = useState('pt_BR')
  const [header, setHeader] = useState('')
  const [body, setBody] = useState('Olá {{1}}, ')
  const [footer, setFooter] = useState('')
  const [sel, setSel] = useState(() => bms.map((b) => b.business_id))
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState(null)
  const [msg, setMsg] = useState(null)

  const vars = varsOf(body)
  const [examples, setExamples] = useState({})

  const toggle = (id) => setSel((s) => s.includes(id) ? s.filter((x) => x !== id) : [...s, id])

  const submit = async () => {
    if (!name.trim() || !body.trim() || sel.length === 0) {
      setMsg({ ok: false, text: 'Preencha nome, corpo e selecione ao menos 1 BM.' }); return
    }
    setBusy(true); setMsg(null); setResults(null)
    try {
      const r = await api.crmCreateTemplate({
        name, category, language,
        header_text: header || null,
        body_text: body,
        body_examples: vars.map((n) => examples[n] || 'exemplo'),
        footer_text: footer || null,
        business_ids: sel,
      })
      setResults(r)
      setMsg({ ok: r.ok > 0, text: `Enviado: ${r.ok}/${r.total} BMs aceitaram. ${r.failed ? `${r.failed} falharam.` : ''}` })
      if (r.ok > 0) setTimeout(onDone, 1800)
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }

  return (
    <Card className="space-y-3">
      <SectionTitle icon={Plus}>Novo template (submeter para aprovação)</SectionTitle>
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="text-[11px] text-neutral-400">Nome (a-z, 0-9, _)</label>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="cadastro_atualizacao" className={`${inputCls} py-2 text-sm`} />
        </div>
        <div>
          <label className="text-[11px] text-neutral-400">Categoria</label>
          <select value={category} onChange={(e) => setCategory(e.target.value)} className={`${inputCls} py-2 text-sm`}>
            <option value="MARKETING">Marketing</option>
            <option value="UTILITY">Utilidade</option>
          </select>
        </div>
        <div>
          <label className="text-[11px] text-neutral-400">Idioma</label>
          <select value={language} onChange={(e) => setLanguage(e.target.value)} className={`${inputCls} py-2 text-sm`}>
            <option value="pt_BR">Português (BR)</option>
            <option value="en_US">Inglês (US)</option>
            <option value="es_ES">Espanhol</option>
          </select>
        </div>
      </div>
      <div>
        <label className="text-[11px] text-neutral-400">Cabeçalho (opcional, texto)</label>
        <input value={header} onChange={(e) => setHeader(e.target.value)} placeholder="Atualização do cadastro" className={`${inputCls} py-2 text-sm`} />
      </div>
      <div>
        <label className="text-[11px] text-neutral-400">Corpo — use {'{{1}}'}, {'{{2}}'}… para variáveis</label>
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} className={`${inputCls} py-2 text-sm`} />
      </div>
      {vars.length > 0 && (
        <div className="rounded-lg border border-neutral-200 dark:border-emerald-400/15 p-3 space-y-2">
          <p className="text-[11px] text-neutral-400">Amostras das variáveis (exigidas pela Meta na aprovação):</p>
          {vars.map((n) => (
            <div key={n} className="flex items-center gap-2">
              <span className="font-mono text-xs w-10">{`{{${n}}}`}</span>
              <input value={examples[n] || ''} onChange={(e) => setExamples((x) => ({ ...x, [n]: e.target.value }))}
                placeholder={`amostra p/ {{${n}}}`} className={`${inputCls} py-1.5 text-sm`} />
            </div>
          ))}
        </div>
      )}
      <div>
        <label className="text-[11px] text-neutral-400">Rodapé (opcional)</label>
        <input value={footer} onChange={(e) => setFooter(e.target.value)} placeholder="Sua empresa" className={`${inputCls} py-2 text-sm`} />
      </div>

      <div>
        <p className="text-[11px] text-neutral-400 mb-1.5">Submeter em quais BMs? ({sel.length}/{bms.length})</p>
        <div className="flex flex-wrap gap-2">
          <button className={`${btnGhost} px-2 py-1 text-[11px]`} onClick={() => setSel(bms.map((b) => b.business_id))}>Todas</button>
          <button className={`${btnGhost} px-2 py-1 text-[11px]`} onClick={() => setSel([])}>Nenhuma</button>
          {bms.map((b) => (
            <button key={b.business_id} onClick={() => toggle(b.business_id)}
              className={`px-2.5 py-1 rounded-full text-[11px] font-semibold border transition ${sel.includes(b.business_id)
                ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                : 'border-neutral-300 dark:border-neutral-700 text-neutral-500'}`}>
              {b.portfolio_name || b.business_id}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button className={`${btnPrimary} px-5 py-2.5 text-sm inline-flex items-center gap-2`} disabled={busy} onClick={submit}>
          {busy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />} Submeter para aprovação
        </button>
        <Toast msg={msg} />
      </div>
      {results && (
        <div className="space-y-1 text-xs">
          {results.results.map((r) => (
            <div key={r.business_id} className="flex items-center gap-2">
              {r.ok ? <CheckCircle2 size={13} className="text-emerald-400" /> : <XCircle size={13} className="text-rose-400" />}
              <span className="font-semibold">{r.portfolio_name || r.business_id}</span>
              <span className="text-neutral-400">{r.ok ? (r.status || 'enviado') : r.error}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Sub-aba: Leads (adicionar, tags, filtrar, selecionar)
// ---------------------------------------------------------------------------
function useLeads() {
  const [leads, setLeads] = useState([])
  const [tags, setTags] = useState([])
  const [loading, setLoading] = useState(true)
  const [tag, setTag] = useState('')
  const [search, setSearch] = useState('')
  const load = async (t = tag, s = search) => {
    setLoading(true)
    try { const r = await api.crmLeads(t, s); setLeads(r.leads || []); setTags(r.tags || []) }
    finally { setLoading(false) }
  }
  useEffect(() => { load(tag, search) }, [tag]) // eslint-disable-line
  return { leads, tags, loading, tag, setTag, search, setSearch, load }
}

function LeadsTab() {
  const L = useLeads()
  const [text, setText] = useState('')
  const [newTags, setNewTags] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [sel, setSel] = useState(new Set())

  const add = async () => {
    if (!text.trim()) return
    setBusy(true); setMsg(null)
    try {
      const r = await api.crmAddLeads({ text, tags: newTags.split(',').map((t) => t.trim()).filter(Boolean) })
      setMsg({ ok: true, text: `+${r.added} novos, ${r.updated} atualizados${r.invalid ? `, ${r.invalid} inválidos` : ''}.` })
      setText(''); L.load()
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  const del = async () => {
    if (sel.size === 0) return
    if (!window.confirm(`Excluir ${sel.size} lead(s)?`)) return
    await api.crmDeleteLeads([...sel]); setSel(new Set()); L.load()
  }
  const toggle = (id) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const allSel = L.leads.length > 0 && sel.size === L.leads.length

  return (
    <div className="space-y-4">
      <SectionTitle icon={Users} hint="Cole telefones, marque com tags e filtre. Os disparos usam esta base (dedup por telefone).">
        Leads
      </SectionTitle>

      <Card className="space-y-2">
        <label className="text-[11px] text-neutral-400">Adicionar leads — uma linha por contato: <span className="font-mono">telefone, nome, tag1|tag2</span></label>
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4}
          placeholder={'5531982120535, Maria Oliveira, vip|sp\n5511998887777, João'} className={`${inputCls} py-2 text-sm font-mono`} />
        <div className="flex items-center gap-2 flex-wrap">
          <input value={newTags} onChange={(e) => setNewTags(e.target.value)} placeholder="tags p/ todos (ex.: setembro, promo)" className={`${inputCls} py-2 text-sm max-w-xs`} />
          <button className={`${btnPrimary} px-4 py-2 text-sm inline-flex items-center gap-2`} disabled={busy} onClick={add}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Adicionar
          </button>
          <Toast msg={msg} />
        </div>
      </Card>

      <Card>
        <div className="flex items-center gap-2 flex-wrap mb-3">
          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-neutral-400" />
            <input value={L.search} onChange={(e) => L.setSearch(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && L.load(L.tag, L.search)}
              placeholder="buscar telefone/nome" className={`${inputCls} py-1.5 text-sm pl-8 max-w-xs`} />
          </div>
          <button className={`${btnGhost} px-2.5 py-1.5 text-xs`} onClick={() => L.load(L.tag, L.search)}>Buscar</button>
          <div className="flex items-center gap-1.5 flex-wrap">
            <button onClick={() => L.setTag('')} className={`px-2.5 py-1 rounded-full text-[11px] font-semibold border ${!L.tag ? 'border-amber-300/60 bg-amber-400/15 text-amber-700 dark:text-amber-300' : 'border-neutral-300 dark:border-neutral-700 text-neutral-500'}`}>todas</button>
            {L.tags.map((t) => (
              <button key={t.tag} onClick={() => L.setTag(t.tag)} className={`px-2.5 py-1 rounded-full text-[11px] font-semibold border inline-flex items-center gap-1 ${L.tag === t.tag ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300' : 'border-neutral-300 dark:border-neutral-700 text-neutral-500'}`}>
                <TagIcon size={10} />{t.tag} <span className="opacity-60">{t.count}</span>
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-neutral-400">{sel.size ? `${sel.size} selecionados` : `${L.leads.length} leads`}</span>
            {sel.size > 0 && <button className={`${btnGhost} px-2.5 py-1.5 text-xs text-rose-500 inline-flex items-center gap-1`} onClick={del}><Trash2 size={13} /> Excluir</button>}
          </div>
        </div>

        {L.loading ? <Skeleton className="h-32" /> : L.leads.length === 0 ? (
          <p className="text-sm text-neutral-400 py-6 text-center">Nenhum lead {L.tag ? `com a tag "${L.tag}"` : 'ainda'}.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-neutral-400 border-b border-neutral-200 dark:border-neutral-800 text-xs uppercase tracking-wide">
                  <th className="px-2 py-2 w-8"><input type="checkbox" checked={allSel} onChange={() => setSel(allSel ? new Set() : new Set(L.leads.map((l) => l.id)))} /></th>
                  <th className="px-2 py-2">Telefone</th><th className="px-2 py-2">Nome</th><th className="px-2 py-2">Tags</th>
                </tr>
              </thead>
              <tbody>
                {L.leads.map((l) => (
                  <LeadRow key={l.id} lead={l} checked={sel.has(l.id)} onToggle={() => toggle(l.id)} onSaved={L.load} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

function LeadRow({ lead, checked, onToggle, onSaved }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(lead.name || '')
  const [tags, setTags] = useState((lead.tags || []).join(', '))
  const save = async () => {
    await api.crmUpdateLead(lead.id, { name, tags: tags.split(',').map((t) => t.trim()).filter(Boolean) })
    setEditing(false); onSaved()
  }
  return (
    <tr className="border-b border-neutral-100 dark:border-neutral-800/60 hover:bg-neutral-50 dark:hover:bg-neutral-800/40">
      <td className="px-2 py-2"><input type="checkbox" checked={checked} onChange={onToggle} /></td>
      <td className="px-2 py-2 font-mono">{fmtPhone(lead.phone)}</td>
      <td className="px-2 py-2">
        {editing ? <input value={name} onChange={(e) => setName(e.target.value)} className={`${inputCls} py-1 text-sm`} /> : (lead.name || <span className="text-neutral-400">—</span>)}
      </td>
      <td className="px-2 py-2">
        {editing ? (
          <div className="flex items-center gap-1.5">
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tag1, tag2" className={`${inputCls} py-1 text-sm`} />
            <button className={`${btnPrimary} px-2 py-1 text-xs`} onClick={save}>ok</button>
            <button className="text-neutral-400 text-xs" onClick={() => setEditing(false)}>×</button>
          </div>
        ) : (
          <div className="flex items-center gap-1 flex-wrap">
            {(lead.tags || []).map((t) => <span key={t} className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 text-[10px] font-semibold">{t}</span>)}
            <button className="text-neutral-400 hover:text-neutral-200 text-[11px] ml-1" onClick={() => setEditing(true)}>editar</button>
          </div>
        )}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Sub-aba: Disparar
// ---------------------------------------------------------------------------
function DispararTab({ bms }) {
  const connected = bms.filter((b) => b.connected)
  const [selBms, setSelBms] = useState(() => new Set(connected.map((b) => b.business_id)))
  const [templates, setTemplates] = useState([])
  const [tplName, setTplName] = useState('')
  const [loadingT, setLoadingT] = useState(false)
  const [perCap, setPerCap] = useState(250)
  const L = useLeads()
  const [selLeads, setSelLeads] = useState(new Set())
  const [bodyParams, setBodyParams] = useState({})
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [msg, setMsg] = useState(null)

  // Fonte dos templates = 1ª BM selecionada (o mesmo nome é enviado a todas).
  const firstBm = [...selBms][0] || ''
  useEffect(() => {
    if (!firstBm) { setTemplates([]); return }
    setLoadingT(true); setTemplates([]); setTplName('')
    api.crmTemplates(firstBm)
      .then((r) => setTemplates((r.templates || []).filter((t) => (t.status || '').toUpperCase() === 'APPROVED')))
      .catch(() => {})
      .finally(() => setLoadingT(false))
  }, [firstBm])

  const tpl = templates.find((t) => t.name === tplName)
  const vars = tpl ? varsOf(bodyOf(tpl)) : []
  const capacity = selBms.size * Number(perCap || 0)
  const toggleBm = (id) => setSelBms((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })

  const dispatch = async () => {
    if (!tpl || selLeads.size === 0 || selBms.size === 0) {
      setMsg({ ok: false, text: 'Escolha BMs, um template aprovado e ao menos 1 lead.' }); return
    }
    setBusy(true); setMsg(null); setResult(null)
    try {
      const body_params = vars.map((n) => {
        const p = bodyParams[n] || { kind: 'static', value: '' }
        return { kind: p.kind, value: p.value }
      })
      const start = await api.crmDispatchMulti({
        business_ids: [...selBms], template_name: tpl.name, language: tpl.language,
        body_params, lead_ids: [...selLeads], per_bm_cap: Number(perCap || 250),
      })
      // Disparo roda em BACKGROUND — acompanha o progresso ao vivo.
      setResult({ ...start, sent: 0, failed: 0, processed: 0, by_bm: [], status: 'running' })
      let job = null
      if (start.job_id) {
        for (let guard = 0; guard < 100000; guard++) {
          await new Promise((res) => setTimeout(res, 1500))
          try { job = await api.crmDispatchStatus(start.job_id) } catch { break }
          setResult(job)
          if (job.status === 'done' || job.status === 'error') break
        }
      } else { job = start }
      if (job) {
        setMsg({
          ok: (job.sent || 0) > 0,
          text: `Enviados ${job.sent || 0}/${job.total ?? job.assigned} em ${job.bms_used} BM(s).` +
            `${job.failed ? ` ${job.failed} falharam.` : ''}` +
            `${job.overflow ? ` ${job.overflow} além da capacidade.` : ''}` +
            `${job.skipped_duplicates ? ` ${job.skipped_duplicates} duplicados.` : ''}` +
            `${job.status === 'error' ? ` ⚠ ${job.error}` : ''}`,
        })
      }
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }

  const allSel = L.leads.length > 0 && selLeads.size === L.leads.length

  return (
    <div className="space-y-4">
      <SectionTitle icon={Rocket} hint="Selecione VÁRIAS BMs — os leads são divididos entre elas (cada uma no seu limite) e disparadas em PARALELO. Escala: 10 BMs × 250 = 2500/leva.">
        Disparar em massa (multi-BM paralelo)
      </SectionTitle>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="space-y-3">
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-[11px] text-neutral-400">BMs para disparar em paralelo ({selBms.size}/{connected.length})</label>
              <div className="flex gap-1.5">
                <button className={`${btnGhost} px-2 py-0.5 text-[11px]`} onClick={() => setSelBms(new Set(connected.map((b) => b.business_id)))}>todas</button>
                <button className={`${btnGhost} px-2 py-0.5 text-[11px]`} onClick={() => setSelBms(new Set())}>nenhuma</button>
              </div>
            </div>
            {connected.length === 0 ? (
              <p className="text-[11px] text-amber-500">Nenhuma BM conectada. Conecte um número na aba "Conectar BMs".</p>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {connected.map((b) => (
                  <button key={b.business_id} onClick={() => toggleBm(b.business_id)}
                    className={`px-2.5 py-1 rounded-full text-[11px] font-semibold border transition ${selBms.has(b.business_id)
                      ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                      : 'border-neutral-300 dark:border-neutral-700 text-neutral-500'}`}>
                    {b.portfolio_name || b.business_id} · {b.display_phone}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="flex items-center gap-3">
            <div className="flex-1">
              <label className="text-[11px] text-neutral-400">Teto por BM / 24h</label>
              <input type="number" min={1} max={100000} value={perCap} onChange={(e) => setPerCap(e.target.value)} className={`${inputCls} py-2 text-sm`} />
            </div>
            <div className="flex-1 rounded-lg border border-emerald-400/25 bg-emerald-500/5 px-3 py-2">
              <p className="text-[10px] text-neutral-400 uppercase">Capacidade da leva</p>
              <p className="text-lg font-bold text-emerald-600 dark:text-emerald-300 tabular-nums">{capacity.toLocaleString('pt-BR')}</p>
            </div>
          </div>

          <div>
            <label className="text-[11px] text-neutral-400">Template aprovado {loadingT && '(carregando…)'}</label>
            <select value={tplName} onChange={(e) => setTplName(e.target.value)} className={`${inputCls} py-2 text-sm`}>
              <option value="">— escolha —</option>
              {templates.map((t) => <option key={t.name} value={t.name}>{t.name} ({t.language})</option>)}
            </select>
            {!loadingT && templates.length === 0 && firstBm && <p className="text-[11px] text-amber-500 mt-1">A 1ª BM selecionada não tem template APROVADO. Aprove o mesmo template em todas na aba Templates.</p>}
            {templates.length > 0 && <p className="text-[10px] text-neutral-400 mt-1">O template precisa estar APROVADO com o mesmo nome/idioma em todas as BMs selecionadas.</p>}
          </div>
          {tpl && (
            <div className="rounded-lg border border-neutral-200 dark:border-emerald-400/15 p-3">
              <p className="text-xs text-neutral-500 dark:text-emerald-100/70 whitespace-pre-wrap">{bodyOf(tpl)}</p>
            </div>
          )}
          {vars.length > 0 && (
            <div className="space-y-2">
              <p className="text-[11px] text-neutral-400">Variáveis do corpo:</p>
              {vars.map((n) => {
                const p = bodyParams[n] || { kind: 'static', value: '' }
                return (
                  <div key={n} className="flex items-center gap-2">
                    <span className="font-mono text-xs w-9">{`{{${n}}}`}</span>
                    <select value={p.kind} onChange={(e) => setBodyParams((x) => ({ ...x, [n]: { ...p, kind: e.target.value } }))} className={`${inputCls} py-1.5 text-sm max-w-[9rem]`}>
                      <option value="static">Texto fixo</option>
                      <option value="name">Nome do lead</option>
                      <option value="phone">Telefone</option>
                    </select>
                    {p.kind === 'static' && (
                      <input value={p.value || ''} onChange={(e) => setBodyParams((x) => ({ ...x, [n]: { ...p, value: e.target.value } }))} placeholder="valor" className={`${inputCls} py-1.5 text-sm`} />
                    )}
                  </div>
                )
              })}
            </div>
          )}
          <div className="flex items-center gap-3 pt-1">
            <button className={`${btnPrimary} px-5 py-2.5 text-sm inline-flex items-center gap-2`} disabled={busy} onClick={dispatch}>
              {busy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />} Disparar {selLeads.size} lead(s) em {selBms.size} BM(s)
            </button>
          </div>
          <Toast msg={msg} />
          {result && (
            <div className="space-y-2">
              {(() => {
                const total = result.total ?? result.assigned ?? 0
                const done = (result.sent || 0) + (result.failed || 0)
                const pct = total > 0 ? Math.round((done / total) * 100) : (result.status === 'done' ? 100 : 0)
                const running = result.status === 'running'
                return (
                  <div>
                    <div className="flex items-center justify-between text-[11px] text-neutral-400 mb-1">
                      <span>{running ? '⏳ disparando…' : result.status === 'error' ? '⚠ erro' : '✅ concluído'} {done}/{total}</span>
                      <span>{pct}%</span>
                    </div>
                    <div className="h-2 w-full rounded-full bg-neutral-200 dark:bg-neutral-800 overflow-hidden">
                      <div className={`h-full transition-all ${result.status === 'error' ? 'bg-rose-500' : 'bg-gradient-to-r from-amber-400 to-emerald-500'}`} style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                )
              })()}
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-lg bg-emerald-500/10 py-1.5"><p className="text-[10px] text-neutral-400">enviados</p><p className="font-bold text-emerald-500">{result.sent}</p></div>
                <div className="rounded-lg bg-rose-500/10 py-1.5"><p className="text-[10px] text-neutral-400">falhas</p><p className="font-bold text-rose-500">{result.failed}</p></div>
                <div className="rounded-lg bg-neutral-500/10 py-1.5"><p className="text-[10px] text-neutral-400">BMs</p><p className="font-bold">{result.bms_used}</p></div>
              </div>
              <div className="max-h-48 overflow-y-auto space-y-1 text-xs">
                {(result.by_bm || []).map((b) => (
                  <div key={b.business_id} className="rounded-lg border border-neutral-200 dark:border-emerald-400/15 px-2.5 py-1.5">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold truncate">{b.name}</span>
                      <span className="text-neutral-400">{b.sent}/{b.assigned} {b.failed ? <span className="text-rose-400">· {b.failed} falha</span> : ''}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>

        <Card>
          <div className="flex items-center gap-2 flex-wrap mb-2">
            <span className="text-xs text-neutral-400">Filtrar por tag:</span>
            <button onClick={() => L.setTag('')} className={`px-2 py-0.5 rounded-full text-[11px] font-semibold border ${!L.tag ? 'border-amber-300/60 bg-amber-400/15 text-amber-700 dark:text-amber-300' : 'border-neutral-300 dark:border-neutral-700 text-neutral-500'}`}>todas</button>
            {L.tags.map((t) => (
              <button key={t.tag} onClick={() => L.setTag(t.tag)} className={`px-2 py-0.5 rounded-full text-[11px] font-semibold border ${L.tag === t.tag ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300' : 'border-neutral-300 dark:border-neutral-700 text-neutral-500'}`}>{t.tag} {t.count}</button>
            ))}
          </div>
          <div className="flex items-center justify-between mb-2">
            <button className="text-xs text-emerald-500 font-semibold" onClick={() => setSelLeads(allSel ? new Set() : new Set(L.leads.map((l) => l.id)))}>
              {allSel ? 'desmarcar todos' : `selecionar todos (${L.leads.length})`}
            </button>
            <span className="text-xs text-neutral-400">{selLeads.size} selecionados</span>
          </div>
          <div className="max-h-[26rem] overflow-y-auto divide-y divide-neutral-100 dark:divide-neutral-800/60">
            {L.loading ? <Skeleton className="h-40" /> : L.leads.length === 0 ? (
              <p className="text-sm text-neutral-400 py-6 text-center">Nenhum lead {L.tag ? `com "${L.tag}"` : ''}. Adicione na aba Leads.</p>
            ) : L.leads.map((l) => (
              <label key={l.id} className="flex items-center gap-2 py-1.5 px-1 cursor-pointer hover:bg-neutral-50 dark:hover:bg-neutral-800/40">
                <input type="checkbox" checked={selLeads.has(l.id)} onChange={() => setSelLeads((s) => { const n = new Set(s); n.has(l.id) ? n.delete(l.id) : n.add(l.id); return n })} />
                <span className="font-mono text-sm">{fmtPhone(l.phone)}</span>
                {l.name && <span className="text-xs text-neutral-400">{l.name}</span>}
                <span className="ml-auto flex gap-1">{(l.tags || []).slice(0, 3).map((t) => <span key={t} className="text-[9px] px-1 rounded bg-emerald-500/10 text-emerald-600 dark:text-emerald-300">{t}</span>)}</span>
              </label>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sub-aba: Chat
// ---------------------------------------------------------------------------
function ChatTab() {
  const [chats, setChats] = useState([])
  const [loading, setLoading] = useState(true)
  const [active, setActive] = useState(null)
  const [messages, setMessages] = useState([])

  const load = async () => {
    setLoading(true)
    try { const r = await api.crmChats(); setChats(r.chats || []) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])
  const open = async (phone) => {
    setActive(phone)
    const r = await api.crmChatMessages(phone); setMessages(r.messages || [])
  }

  return (
    <div className="space-y-4">
      <SectionTitle icon={MessageSquare} hint="Histórico das mensagens disparadas (e respostas, quando o webhook estiver ligado).">
        Conversas
      </SectionTitle>
      <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
        <Card className="p-0 overflow-hidden">
          <div className="max-h-[32rem] overflow-y-auto divide-y divide-neutral-100 dark:divide-neutral-800/60">
            {loading ? <div className="p-3"><Skeleton className="h-24" /></div> : chats.length === 0 ? (
              <p className="text-sm text-neutral-400 py-8 text-center">Nenhuma conversa ainda. Dispare um template.</p>
            ) : chats.map((c) => (
              <button key={c.lead_phone} onClick={() => open(c.lead_phone)}
                className={`w-full text-left px-3 py-2.5 transition ${active === c.lead_phone ? 'bg-emerald-500/10' : 'hover:bg-neutral-50 dark:hover:bg-neutral-800/40'}`}>
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-sm">{c.name || fmtPhone(c.lead_phone)}</span>
                  <span className="text-[10px] text-neutral-400">{c.total} msg</span>
                </div>
                <p className="text-xs text-neutral-400 truncate">{c.last_body || '—'}</p>
              </button>
            ))}
          </div>
        </Card>
        <Card>
          {!active ? (
            <p className="text-sm text-neutral-400 py-12 text-center">Selecione uma conversa à esquerda.</p>
          ) : (
            <div>
              <div className="flex items-center justify-between mb-3 pb-2 border-b border-neutral-200 dark:border-neutral-800">
                <span className="font-bold">{fmtPhone(active)}</span>
                <CopyButton text={active} label="copiar número" className="text-xs text-neutral-400 hover:text-neutral-200" />
              </div>
              <div className="space-y-2 max-h-[26rem] overflow-y-auto">
                {messages.map((m) => (
                  <div key={m.id} className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm ${m.direction === 'out'
                    ? 'ml-auto bg-emerald-500/15 border border-emerald-400/25'
                    : 'mr-auto bg-neutral-100 dark:bg-neutral-800'}`}>
                    <p className="whitespace-pre-wrap">{m.body || <span className="italic text-neutral-400">template: {m.template_name}</span>}</p>
                    <div className="flex items-center gap-2 mt-1 text-[10px] text-neutral-400">
                      <span>{m.template_name}</span>
                      {m.error ? <span className="text-rose-400">falha: {m.error}</span> : <span className="text-emerald-500">{m.status || 'enviado'}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Página principal
// ---------------------------------------------------------------------------
export default function Crm() {
  const [tab, setTab] = useState('bms')
  const [bms, setBms] = useState([])
  const [loading, setLoading] = useState(true)
  const [stats, setStats] = useState(null)

  const loadBms = async () => {
    setLoading(true)
    try { const r = await api.crmBms(); setBms(r.bms || []) } finally { setLoading(false) }
  }
  const loadStats = async () => { try { setStats(await api.crmStats()) } catch { /* ignore */ } }
  useEffect(() => { loadBms(); loadStats() }, [])

  const connectedCount = useMemo(() => bms.filter((b) => b.connected).length, [bms])

  return (
    <div>
      <PageHead title="CRM · WhatsApp" subtitle="API oficial (Cloud API) — conecte BMs, aprove templates e dispare para seus leads">
        <button className={`${btnGhost} px-3 py-2 text-sm inline-flex items-center gap-2`} onClick={() => { loadBms(); loadStats() }}>
          <RefreshCw size={14} /> Atualizar
        </button>
      </PageHead>

      <div className="grid gap-3 grid-cols-2 lg:grid-cols-4 mb-5">
        <Stat label="BMs conectadas" value={connectedCount} hint={`${bms.length} com WhatsApp`} accent />
        <Stat label="Leads" value={stats?.leads ?? '—'} />
        <Stat label="Mensagens enviadas" value={stats?.sent ?? '—'} positive />
        <Stat label="Falhas" value={stats?.failed ?? '—'} danger />
      </div>

      <div className="flex items-center gap-1.5 mb-5 flex-wrap">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setTab(key)}
            className={`inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-sm font-bold uppercase tracking-wide transition ${tab === key
              ? 'bg-neutral-900 text-white dark:[background:linear-gradient(95deg,#e8c14a,#34d399)] dark:text-[#06130d] dark:shadow-[0_0_18px_-5px_rgba(52,211,153,0.6)]'
              : 'text-neutral-500 dark:text-emerald-100/70 hover:bg-neutral-100 dark:hover:bg-emerald-400/10 border border-neutral-200 dark:border-emerald-400/15'}`}>
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === 'bms' && <BmsTab bms={bms} loading={loading} reload={() => { loadBms(); loadStats() }} />}
      {tab === 'templates' && <TemplatesTab bms={bms} />}
      {tab === 'disparar' && <DispararTab bms={bms} />}
      {tab === 'leads' && <LeadsTab />}
      {tab === 'chat' && <ChatTab />}
    </div>
  )
}

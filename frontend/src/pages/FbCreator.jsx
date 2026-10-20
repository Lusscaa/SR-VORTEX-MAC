import { useState, useEffect, useRef, useCallback } from 'react'
import { PageHead, Card, Stat, Badge, btnPrimary, btnGhost, inputCls } from '../components/ui.jsx'

const API_BASE = '/api/fb-creator'

async function apiFetch(path, opts = {}) {
  const token = localStorage.getItem('panel_token')
  const headers = { ...(opts.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch {}
    throw new Error(detail)
  }
  return res.json()
}

const STATUS_LABELS = {
  queued: 'Na fila',
  creating_profile: 'Criando perfil',
  opening_browser: 'Abrindo browser',
  registering: 'Registrando',
  waiting_sms: 'Aguardando SMS',
  solving_captcha: 'Resolvendo CAPTCHA',
  warmup: 'Warm-up',
  completed: 'Concluído',
  failed: 'Falhou',
  cancelled: 'Cancelado',
}

const STATUS_TONE = {
  queued: 'neutral',
  creating_profile: 'running',
  opening_browser: 'running',
  registering: 'running',
  waiting_sms: 'running',
  solving_captcha: 'running',
  warmup: 'running',
  completed: 'green',
  failed: 'danger',
  cancelled: 'amber',
}

/* ── SMS presets por provedor ─────────────────────────────── */

const NV_PRESETS = {
  facebook: {
    label: 'Facebook',
    description: 'Serviço Facebook / Vivo — ~R$1,30',
    service_id: 60033, service_name: 'Facebook', service_code: 'fb',
    purchase_api_id: 1, target_price: 130, operator: 'vivo',
    group: 'facebook',
  },
  other_1: {
    label: 'Outros Apps (Opção 1)',
    description: 'Outros apps/Site — operadora aleatória — ~R$2,00',
    service_id: 60009, service_name: 'Outros apps/Site', service_code: 'ot',
    purchase_api_id: 1, target_price: 200, operator: '',
    group: 'outros',
  },
  other_2: {
    label: 'Outros Apps (Opção 2)',
    description: 'Outros apps/Site — operadora aleatória — ~R$1,80',
    service_id: 60009, service_name: 'Outros apps/Site', service_code: 'ot',
    purchase_api_id: 2, target_price: 180, operator: '',
    group: 'outros',
  },
  other_3: {
    label: 'Outros Apps (Opção 3)',
    description: 'Outros apps/Site — operadora aleatória — ~R$2,70',
    service_id: 60009, service_name: 'Outros apps/Site', service_code: 'ot',
    purchase_api_id: 3, target_price: 270, operator: '',
    group: 'outros',
  },
}

const NV_OPERATORS = [
  { value: '', label: 'Qualquer operadora' },
  { value: 'vivo', label: 'Vivo' },
  { value: 'claro', label: 'Claro' },
  { value: 'tim', label: 'TIM' },
  { value: 'oi', label: 'Oi' },
]

const SMS24H_DDD_OPTIONS = [
  { value: '', label: 'Qualquer DDD' },
  { value: '11', label: '11 — São Paulo' },
  { value: '21', label: '21 — Rio de Janeiro' },
  { value: '31', label: '31 — Belo Horizonte' },
  { value: '41', label: '41 — Curitiba' },
  { value: '51', label: '51 — Porto Alegre' },
  { value: '61', label: '61 — Brasília' },
  { value: '71', label: '71 — Salvador' },
  { value: '81', label: '81 — Recife' },
  { value: '85', label: '85 — Fortaleza' },
  { value: '91', label: '91 — Belém' },
  { value: '19', label: '19 — Campinas' },
  { value: '27', label: '27 — Vitória' },
  { value: '47', label: '47 — Joinville' },
  { value: '48', label: '48 — Florianópolis' },
  { value: '62', label: '62 — Goiânia' },
  { value: '65', label: '65 — Cuiabá' },
  { value: '67', label: '67 — Campo Grande' },
  { value: '79', label: '79 — Aracaju' },
  { value: '82', label: '82 — Maceió' },
  { value: '84', label: '84 — Natal' },
  { value: '86', label: '86 — Teresina' },
  { value: '92', label: '92 — Manaus' },
  { value: '98', label: '98 — São Luís' },
]

/* ── Tiny helpers ──────────────────────────────────────────── */

function SectionTitle({ children }) {
  return (
    <h3 className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-widest text-neutral-400 dark:text-emerald-300/50 mb-3 mt-1 select-none">
      <span className="h-px flex-1 bg-neutral-200 dark:bg-emerald-400/15" />
      {children}
      <span className="h-px flex-1 bg-neutral-200 dark:bg-emerald-400/15" />
    </h3>
  )
}

function Chip({ active, onClick, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all border
        ${active
          ? 'border-emerald-500 dark:border-amber-400/70 bg-emerald-50 dark:bg-amber-400/10 text-emerald-700 dark:text-amber-300 shadow-sm'
          : 'border-neutral-200 dark:border-emerald-400/15 bg-transparent text-neutral-500 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-emerald-400/5'
        }`}
    >
      {children}
    </button>
  )
}

function FieldLabel({ children, hint }) {
  return (
    <label className="block text-xs font-semibold text-neutral-500 dark:text-neutral-400 mb-1.5">
      {children}
      {hint && <span className="ml-1.5 font-normal text-neutral-400 dark:text-neutral-500">{hint}</span>}
    </label>
  )
}

/* ── Tab switcher ──────────────────────────────────────────── */

function Tabs({ tabs, active, onChange }) {
  return (
    <div className="flex border-b border-neutral-200 dark:border-emerald-400/15 mb-4">
      {tabs.map(t => (
        <button
          key={t.key}
          type="button"
          onClick={() => onChange(t.key)}
          className={`px-4 py-2 text-xs font-bold uppercase tracking-wide transition-all border-b-2 -mb-px
            ${active === t.key
              ? 'border-emerald-500 dark:border-amber-400 text-neutral-900 dark:text-amber-300'
              : 'border-transparent text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300'
            }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

/* ── Toggle switch ────────────────────────────────────────── */

const TOGGLE_COLORS = {
  emerald: {
    track: 'bg-emerald-500 dark:bg-emerald-400',
    glow: 'shadow-[0_0_8px_rgba(16,185,129,0.4)] dark:shadow-[0_0_8px_rgba(52,211,153,0.3)]',
    dot: 'bg-white',
  },
  blue: {
    track: 'bg-blue-500 dark:bg-blue-400',
    glow: 'shadow-[0_0_8px_rgba(59,130,246,0.4)] dark:shadow-[0_0_8px_rgba(96,165,250,0.3)]',
    dot: 'bg-white',
  },
  amber: {
    track: 'bg-amber-500 dark:bg-amber-400',
    glow: 'shadow-[0_0_8px_rgba(245,158,11,0.4)] dark:shadow-[0_0_8px_rgba(251,191,36,0.3)]',
    dot: 'bg-white',
  },
}

function Toggle({ active, onChange, label, description, color = 'emerald' }) {
  const c = TOGGLE_COLORS[color] || TOGGLE_COLORS.emerald
  return (
    <button
      type="button"
      onClick={() => onChange(!active)}
      className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg border transition-all text-left group
        ${active
          ? 'border-neutral-200 dark:border-emerald-400/20 bg-neutral-50 dark:bg-emerald-400/5'
          : 'border-neutral-100 dark:border-neutral-800 bg-transparent hover:bg-neutral-50 dark:hover:bg-neutral-800/30'
        }`}
    >
      <div className={`relative w-10 h-[22px] rounded-full transition-all shrink-0 ${active ? `${c.track} ${c.glow}` : 'bg-neutral-300 dark:bg-neutral-700'}`}>
        <div className={`absolute top-[3px] left-[3px] h-4 w-4 rounded-full ${c.dot} shadow-sm transition-transform duration-200 ${active ? 'translate-x-[18px]' : ''}`} />
      </div>
      <div className="min-w-0">
        <span className={`block text-sm font-semibold transition-colors ${active ? 'text-neutral-800 dark:text-neutral-100' : 'text-neutral-600 dark:text-neutral-400'}`}>
          {label}
        </span>
        {description && (
          <span className="block text-[10px] text-neutral-400 dark:text-neutral-500 leading-tight mt-0.5">
            {description}
          </span>
        )}
      </div>
    </button>
  )
}

/* ── Main component ────────────────────────────────────────── */

export default function FbCreator() {
  const [stats, setStats] = useState(null)
  const [jobs, setJobs] = useState([])
  const [accounts, setAccounts] = useState([])
  const [selectedJob, setSelectedJob] = useState(null)
  const [jobLogs, setJobLogs] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState('jobs')

  // ── Config state ──
  const [count, setCount] = useState(1)
  const [concurrent, setConcurrent] = useState(1)
  const [osType, setOsType] = useState('android')
  const [browserType, setBrowserType] = useState('chromium')
  const [gender, setGender] = useState('random')
  const [proxyMode, setProxyMode] = useState('none')
  const [proxiesText, setProxiesText] = useState('')
  const [useSms, setUseSms] = useState(false)
  const [smsPreset, setSmsPreset] = useState('facebook')
  const [smsProvider, setSmsProvider] = useState('numero_virtual')
  const [smsOperator, setSmsOperator] = useState('vivo')
  const [smsTimeout, setSmsTimeout] = useState(240)
  const [smsBalance, setSmsBalance] = useState({})
  const [smsDdd, setSmsDdd] = useState('')
  const [useCustomEmails, setUseCustomEmails] = useState(false)
  const [customEmailsText, setCustomEmailsText] = useState('')

  const logRef = useRef(null)
  const pollRef = useRef(null)

  // ── Polling ──
  const refresh = useCallback(async () => {
    try {
      const [s, j] = await Promise.all([
        apiFetch('/stats'),
        apiFetch('/jobs'),
      ])
      setStats(s)
      setJobs(j)
    } catch {}
  }, [])

  const refreshAccounts = useCallback(async () => {
    try {
      const a = await apiFetch('/accounts')
      setAccounts(a)
    } catch {}
  }, [])

  const refreshBalance = useCallback(async () => {
    try {
      const [nv, s24] = await Promise.allSettled([
        apiFetch('/sms-balance?provider=numero_virtual'),
        apiFetch('/sms-balance?provider=sms24h_org'),
      ])
      setSmsBalance(prev => ({
        ...prev,
        numero_virtual: nv.status === 'fulfilled' ? nv.value : prev?.numero_virtual,
        sms24h_org: s24.status === 'fulfilled' ? s24.value : prev?.sms24h_org,
      }))
    } catch {}
  }, [])

  useEffect(() => {
    refresh()
    refreshAccounts()
    refreshBalance()
    pollRef.current = setInterval(refresh, 3000)
    return () => clearInterval(pollRef.current)
  }, [refresh, refreshAccounts, refreshBalance])

  useEffect(() => {
    if (!selectedJob) return
    let active = true
    const poll = async () => {
      try {
        const data = await apiFetch(`/jobs/${selectedJob}/logs`)
        if (active) setJobLogs(data.logs || [])
      } catch {}
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => { active = false; clearInterval(id) }
  }, [selectedJob])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [jobLogs])

  // ── Handlers ──
  const handleStart = async () => {
    setLoading(true)
    setError('')
    try {
      const proxies = proxyMode !== 'none' && proxiesText.trim()
        ? proxiesText.trim().split('\n').map(line => {
            const parts = line.split(':')
            return { host: parts[0], port: parts[1], user: parts[2], pass: parts[3] }
          })
        : []
      const customEmails = useCustomEmails && customEmailsText.trim()
        ? customEmailsText.trim().split('\n').map(e => e.trim()).filter(Boolean)
        : []
      const smsPayload = {}
      if (useSms) {
        smsPayload.use_sms = true
        smsPayload.sms_provider = smsProvider
        smsPayload.sms_timeout = smsTimeout
        if (smsProvider === 'numero_virtual') {
          const preset = NV_PRESETS[smsPreset] || NV_PRESETS.facebook
          smsPayload.sms_service_name = preset.service_name
          smsPayload.sms_service_id = preset.service_id
          smsPayload.sms_target_price = preset.target_price
          smsPayload.sms_purchase_api_id = preset.purchase_api_id
          smsPayload.sms_operator = smsOperator
        } else {
          smsPayload.sms_service_name = 'Facebook'
          smsPayload.sms_service_id = 0
          smsPayload.sms_ddd = smsDdd
        }
      }
      await apiFetch('/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          count: customEmails.length > 0 ? Math.max(count, customEmails.length) : count,
          max_concurrent: concurrent,
          os_type: osType,
          gender,
          proxy_mode: proxyMode,
          proxies,
          ...smsPayload,
          browser_type: browserType,
          custom_emails: customEmails,
        }),
      })
      setActiveTab('jobs')
      await refresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleStop = async () => {
    try {
      await apiFetch('/stop', { method: 'POST' })
      await refresh()
    } catch (e) {
      setError(e.message)
    }
  }

  const isRunning = stats?.is_running

  return (
    <>
      <PageHead title="FB-Creator" subtitle="Criação automatizada de contas Facebook" />

      {error && (
        <div className="mb-4 rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 px-4 py-2.5 text-sm text-red-700 dark:text-red-300 flex items-center justify-between">
          <span>{error}</span>
          <button type="button" onClick={() => setError('')} className="ml-3 font-bold text-red-400 hover:text-red-600">&times;</button>
        </div>
      )}

      {/* ── Stats ── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-6">
        <Stat label="Total" value={stats?.total ?? '—'} accent />
        <Stat label="Rodando" value={stats?.running ?? '—'} />
        <Stat label="Na fila" value={stats?.queued ?? '—'} />
        <Stat label="Concluídos" value={stats?.completed ?? '—'} positive />
        <Stat label="Falhas" value={stats?.failed ?? '—'} danger />
      </div>

      <div className="grid lg:grid-cols-[360px_1fr] gap-6">

        {/* ╔══════════════════════════════════════════╗ */}
        {/* ║  PAINEL DE CONFIGURAÇÃO                  ║ */}
        {/* ╚══════════════════════════════════════════╝ */}
        <Card>
          <h2 className="font-bold text-sm uppercase tracking-wide text-neutral-500 dark:text-emerald-300/70 mb-4">
            Configuração
          </h2>

          {/* ── Navegador ── */}
          <SectionTitle>Navegador</SectionTitle>
          <div className="flex gap-2 mb-4">
            <Chip active={browserType === 'chromium'} onClick={() => setBrowserType('chromium')}>
              🌐 Google Chromium
            </Chip>
            <Chip active={browserType === 'ixbrowser'} onClick={() => setBrowserType('ixbrowser')}>
              🛡️ IxBrowser
            </Chip>
          </div>
          <p className="text-[10px] text-neutral-400 dark:text-neutral-500 mb-4 leading-relaxed">
            {browserType === 'chromium'
              ? 'Chromium nativo via Playwright. Rápido, sem dependência externa. Bom para volume.'
              : 'IxBrowser antidetect com fingerprint único por perfil. Requer IxBrowser aberto.'}
          </p>

          {/* ── Plataforma ── */}
          <SectionTitle>Plataforma</SectionTitle>
          <div className="flex gap-2 mb-4">
            <Chip active={osType === 'android'} onClick={() => setOsType('android')}>
              📱 Android
            </Chip>
            <Chip active={osType === 'windows'} onClick={() => setOsType('windows')}>
              🖥️ Desktop
            </Chip>
            <Chip active={osType === 'ios'} onClick={() => setOsType('ios')}>
              🍎 iOS
            </Chip>
          </div>

          {/* ── Quantidade ── */}
          <SectionTitle>Batch</SectionTitle>
          <div className="grid grid-cols-2 gap-3 mb-4">
            <div>
              <FieldLabel hint="(1–50)">Quantidade</FieldLabel>
              <input type="number" min={1} max={50} value={count}
                onChange={e => setCount(+e.target.value || 1)} className={inputCls} />
            </div>
            <div>
              <FieldLabel hint="(1–10)">Simultâneos</FieldLabel>
              <input type="number" min={1} max={10} value={concurrent}
                onChange={e => setConcurrent(+e.target.value || 1)} className={inputCls} />
            </div>
          </div>

          {/* ── Identidade ── */}
          <SectionTitle>Identidade</SectionTitle>
          <div className="mb-4">
            <FieldLabel>Gênero</FieldLabel>
            <div className="flex gap-2">
              <Chip active={gender === 'random'} onClick={() => setGender('random')}>🎲 Aleatório</Chip>
              <Chip active={gender === 'male'} onClick={() => setGender('male')}>♂ Masculino</Chip>
              <Chip active={gender === 'female'} onClick={() => setGender('female')}>♀ Feminino</Chip>
            </div>
          </div>

          {/* ── Proxy ── */}
          <SectionTitle>Proxy</SectionTitle>
          <div className="mb-3">
            <div className="flex gap-2 mb-3">
              <Chip active={proxyMode === 'none'} onClick={() => setProxyMode('none')}>Sem proxy</Chip>
              <Chip active={proxyMode === 'unit'} onClick={() => setProxyMode('unit')}>1 por conta</Chip>
              <Chip active={proxyMode === 'rotate'} onClick={() => setProxyMode('rotate')}>Rotacionar</Chip>
            </div>
            {proxyMode !== 'none' && (
              <div>
                <FieldLabel hint="host:port:user:pass">Lista de Proxies</FieldLabel>
                <textarea
                  value={proxiesText}
                  onChange={e => setProxiesText(e.target.value)}
                  rows={3}
                  placeholder={'host:port:user:pass\nhost:port:user:pass'}
                  className={inputCls + ' text-xs font-mono resize-none'}
                />
              </div>
            )}
          </div>

          {/* ── Contato / Verificação ── */}
          <SectionTitle>Contato &amp; Verificação</SectionTitle>
          <div className="space-y-3 mb-4">

            {/* Toggle: E-mails customizados */}
            <Toggle
              active={useCustomEmails}
              onChange={setUseCustomEmails}
              label="Usar lista de e-mails"
              description="Cada conta usará um e-mail da sua lista"
              color="blue"
            />
            {useCustomEmails && (
              <div className="pl-1 ml-1 border-l-2 border-blue-200 dark:border-blue-400/20">
                <div className="pl-3">
                  <FieldLabel hint="(1 por linha)">Lista de E-mails</FieldLabel>
                  <textarea
                    value={customEmailsText}
                    onChange={e => setCustomEmailsText(e.target.value)}
                    rows={4}
                    placeholder={'joao.silva@gmail.com\nmaria.santos@outlook.com\npedro.costa@yahoo.com'}
                    className={inputCls + ' text-xs font-mono resize-none'}
                  />
                  {customEmailsText.trim() && (
                    <p className="mt-1 text-[10px] text-blue-500 dark:text-blue-400">
                      {customEmailsText.trim().split('\n').filter(l => l.trim()).length} e-mail(s) na lista
                    </p>
                  )}
                </div>
              </div>
            )}

            {/* Toggle: Verificação SMS */}
            <Toggle
              active={useSms}
              onChange={setUseSms}
              label="Verificação SMS"
              description="Usar número virtual para verificar a conta"
              color="emerald"
            />
            {useSms && (() => {
              const balNv  = smsBalance?.numero_virtual
              const balS24 = smsBalance?.sms24h_org
              const balClr = (b) => b?.balance > 2
                ? 'text-emerald-600 dark:text-emerald-400'
                : b?.balance > 0
                ? 'text-amber-600 dark:text-amber-400'
                : 'text-red-500 dark:text-red-400'
              const fbPresets = Object.entries(NV_PRESETS).filter(([, p]) => p.group === 'facebook')
              const otPresets = Object.entries(NV_PRESETS).filter(([, p]) => p.group === 'outros')
              return (
              <div className="pl-1 ml-1 border-l-2 border-emerald-200 dark:border-emerald-400/20 space-y-3">

                {/* ── Saldo dos Provedores ── */}
                <div className="ml-3 rounded-lg border border-neutral-200 dark:border-emerald-400/15 bg-neutral-50/60 dark:bg-emerald-950/20 p-2.5">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[10px] font-bold uppercase tracking-wider text-neutral-400 dark:text-neutral-500">Saldo SMS</span>
                    <button type="button" onClick={refreshBalance}
                      className="text-[10px] text-neutral-400 hover:text-emerald-500 dark:hover:text-emerald-400 transition-colors"
                      title="Atualizar saldo">↻ atualizar</button>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div className="flex flex-col items-center rounded-md bg-white/70 dark:bg-neutral-900/50 px-2 py-1.5 border border-neutral-100 dark:border-neutral-800">
                      <span className="text-[9px] text-neutral-400 dark:text-neutral-500 mb-0.5">Numero Virtual</span>
                      <span className={`text-sm font-bold ${balNv?.balance != null ? balClr(balNv) : 'text-neutral-300 dark:text-neutral-600'}`}>
                        {balNv?.balance != null ? `R$ ${balNv.balance.toFixed(2)}` : '—'}
                      </span>
                    </div>
                    <div className="flex flex-col items-center rounded-md bg-white/70 dark:bg-neutral-900/50 px-2 py-1.5 border border-neutral-100 dark:border-neutral-800">
                      <span className="text-[9px] text-neutral-400 dark:text-neutral-500 mb-0.5">SMS24h.org</span>
                      <span className={`text-sm font-bold ${balS24?.balance != null ? balClr(balS24) : 'text-neutral-300 dark:text-neutral-600'}`}>
                        {balS24?.balance != null ? `R$ ${balS24.balance.toFixed(2)}` : '—'}
                      </span>
                    </div>
                  </div>
                </div>

                {/* ── Provedor ── */}
                <div className="pl-3">
                  <FieldLabel>Provedor SMS</FieldLabel>
                  <select value={smsProvider} onChange={e => {
                    setSmsProvider(e.target.value)
                    if (e.target.value === 'numero_virtual') {
                      setSmsPreset('facebook')
                      setSmsOperator('vivo')
                    }
                  }} className={inputCls}>
                    <option value="numero_virtual">Numero Virtual</option>
                    <option value="sms24h_org">SMS24h.org</option>
                  </select>
                </div>

                {/* ── Opções Numero Virtual ── */}
                {smsProvider === 'numero_virtual' && (
                  <>
                    <div className="pl-3 space-y-2.5">
                      {/* Facebook */}
                      <div>
                        <FieldLabel>Facebook</FieldLabel>
                        <div className="space-y-0.5">
                          {fbPresets.map(([key, p]) => (
                            <label key={key} className="flex items-center gap-2 cursor-pointer group px-2.5 py-1.5 rounded-md hover:bg-neutral-50 dark:hover:bg-neutral-800/40 transition-colors">
                              <input type="radio" name="smsPreset" value={key}
                                checked={smsPreset === key}
                                onChange={() => { setSmsPreset(key); if (p.operator) setSmsOperator(p.operator) }}
                                className="accent-emerald-500 dark:accent-amber-400" />
                              <span className="text-xs font-semibold text-neutral-700 dark:text-neutral-200 group-hover:text-neutral-900 dark:group-hover:text-neutral-100 flex-1">
                                {p.label}
                              </span>
                              <span className="text-[10px] text-neutral-400 dark:text-neutral-500">
                                ~R${(p.target_price / 100).toFixed(2).replace('.', ',')}
                              </span>
                            </label>
                          ))}
                        </div>
                      </div>
                      {/* Outros Apps */}
                      <div>
                        <FieldLabel>Outros Apps / Site</FieldLabel>
                        <div className="space-y-0.5">
                          {otPresets.map(([key, p]) => (
                            <label key={key} className="flex items-center gap-2 cursor-pointer group px-2.5 py-1.5 rounded-md hover:bg-neutral-50 dark:hover:bg-neutral-800/40 transition-colors">
                              <input type="radio" name="smsPreset" value={key}
                                checked={smsPreset === key}
                                onChange={() => { setSmsPreset(key); if (p.operator) setSmsOperator(p.operator) }}
                                className="accent-emerald-500 dark:accent-amber-400" />
                              <span className="text-xs font-semibold text-neutral-700 dark:text-neutral-200 group-hover:text-neutral-900 dark:group-hover:text-neutral-100 flex-1">
                                {p.label}
                              </span>
                              <span className="text-[10px] text-neutral-400 dark:text-neutral-500">
                                ~R${(p.target_price / 100).toFixed(2).replace('.', ',')}
                              </span>
                            </label>
                          ))}
                        </div>
                      </div>
                    </div>
                    <div className="pl-3">
                      <FieldLabel>Operadora</FieldLabel>
                      <select value={smsOperator} onChange={e => setSmsOperator(e.target.value)} className={inputCls}>
                        {NV_OPERATORS.map(op => (
                          <option key={op.value} value={op.value}>{op.label}</option>
                        ))}
                      </select>
                    </div>
                  </>
                )}

                {/* ── Opções SMS24h.org ── */}
                {smsProvider === 'sms24h_org' && (
                  <>
                    <div className="pl-3">
                      <div className="flex items-center gap-2 px-3 py-2 rounded-lg border border-emerald-200 dark:border-emerald-400/20 bg-emerald-50/50 dark:bg-emerald-400/5">
                        <span className="text-xs font-semibold text-neutral-700 dark:text-neutral-200">Facebook</span>
                        <span className="text-[10px] text-neutral-400 dark:text-neutral-500">— ~R$0,80</span>
                      </div>
                    </div>
                    <div className="pl-3">
                      <FieldLabel>DDD</FieldLabel>
                      <select value={smsDdd} onChange={e => setSmsDdd(e.target.value)} className={inputCls}>
                        {SMS24H_DDD_OPTIONS.map(d => (
                          <option key={d.value} value={d.value}>{d.label}</option>
                        ))}
                      </select>
                    </div>
                  </>
                )}

                {/* Timeout */}
                <div className="pl-3">
                  <FieldLabel hint="(60–600)">Timeout (seg)</FieldLabel>
                  <input type="number" min={60} max={600} step={30} value={smsTimeout}
                    onChange={e => setSmsTimeout(+e.target.value || 240)} className={inputCls} />
                </div>
              </div>
              )
            })()}
          </div>

          {/* ── Botão de ação ── */}
          <div className="pt-2 border-t border-neutral-100 dark:border-emerald-400/10">
            {!isRunning ? (
              <button onClick={handleStart} disabled={loading}
                className={`${btnPrimary} w-full px-4 py-2.5 text-sm font-bold`}>
                {loading ? 'Iniciando…' : `Criar ${count} conta${count > 1 ? 's' : ''}`}
              </button>
            ) : (
              <button onClick={handleStop}
                className={`${btnGhost} w-full px-4 py-2.5 text-sm font-bold text-red-600 dark:text-red-400 border-red-300 dark:border-red-800`}>
                Parar Tudo
              </button>
            )}
            {isRunning && (
              <p className="mt-2 text-center text-[10px] text-neutral-400 dark:text-neutral-500 animate-pulse">
                Batch em execução…
              </p>
            )}
          </div>
        </Card>

        {/* ╔══════════════════════════════════════════╗ */}
        {/* ║  PAINEL PRINCIPAL (tabs)                 ║ */}
        {/* ╚══════════════════════════════════════════╝ */}
        <div>
          <Tabs
            tabs={[
              { key: 'jobs', label: `Jobs${jobs.length ? ` (${jobs.length})` : ''}` },
              { key: 'accounts', label: `Contas${accounts.length ? ` (${accounts.length})` : ''}` },
            ]}
            active={activeTab}
            onChange={k => { setActiveTab(k); if (k === 'accounts') refreshAccounts() }}
          />

          {/* ── Tab: Jobs ── */}
          {activeTab === 'jobs' && (
            <div className="space-y-4">
              <Card>
                <h2 className="font-bold text-sm uppercase tracking-wide text-neutral-500 dark:text-emerald-300/70 mb-3">
                  Jobs em execução
                </h2>
                {jobs.length === 0 ? (
                  <div className="text-center py-8">
                    <p className="text-neutral-400 dark:text-neutral-500 text-sm">Nenhum job ainda</p>
                    <p className="text-neutral-300 dark:text-neutral-600 text-xs mt-1">Configure e clique "Criar" para começar</p>
                  </div>
                ) : (
                  <div className="overflow-x-auto max-h-[340px] overflow-y-auto">
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 bg-white dark:bg-[#0a1a12]">
                        <tr className="text-left text-neutral-400 border-b border-neutral-200 dark:border-neutral-800 text-xs uppercase tracking-wide">
                          <th className="px-2 py-1.5 font-medium">ID</th>
                          <th className="px-2 py-1.5 font-medium">Nome</th>
                          <th className="px-2 py-1.5 font-medium">Status</th>
                          <th className="px-2 py-1.5 font-medium">OS</th>
                          <th className="px-2 py-1.5 font-medium">E-mail</th>
                        </tr>
                      </thead>
                      <tbody>
                        {jobs.map(j => (
                          <tr
                            key={j.job_id}
                            onClick={() => setSelectedJob(j.job_id)}
                            className={`border-b border-neutral-100 dark:border-neutral-800/60 cursor-pointer transition ${
                              selectedJob === j.job_id
                                ? 'bg-neutral-100 dark:bg-emerald-400/10'
                                : 'hover:bg-neutral-50 dark:hover:bg-neutral-800/40'
                            }`}
                          >
                            <td className="px-2 py-1.5 font-mono text-xs">{j.job_id?.slice(0, 8)}</td>
                            <td className="px-2 py-1.5 font-medium">{j.first_name} {j.last_name}</td>
                            <td className="px-2 py-1.5">
                              <Badge tone={STATUS_TONE[j.status] || 'neutral'}>
                                {STATUS_LABELS[j.status] || j.status}
                              </Badge>
                            </td>
                            <td className="px-2 py-1.5 text-xs">
                              {j.os_type === 'android' ? '📱' : j.os_type === 'ios' ? '🍎' : '🖥️'}
                            </td>
                            <td className="px-2 py-1.5 text-xs text-neutral-500">{j.email || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              {/* Console de logs */}
              <Card>
                <h2 className="font-bold text-sm uppercase tracking-wide text-neutral-500 dark:text-emerald-300/70 mb-3">
                  {selectedJob ? `Log — ${selectedJob.slice(0, 8)}` : 'Log'}
                </h2>
                <div
                  ref={logRef}
                  className="h-[280px] overflow-y-auto rounded-lg border border-neutral-200 dark:border-emerald-400/15 bg-neutral-50 dark:bg-[#040e08] p-3 font-mono text-xs leading-relaxed"
                >
                  {!selectedJob ? (
                    <p className="text-neutral-400 text-center mt-20">Selecione um job para ver os logs</p>
                  ) : jobLogs.length === 0 ? (
                    <p className="text-neutral-400 text-center mt-20">Sem logs ainda…</p>
                  ) : (
                    jobLogs.map((line, i) => (
                      <div key={i} className={`py-px ${
                        line.includes('ERRO') ? 'text-red-400' :
                        line.includes('sucesso') || line.includes('Concluído') || line.includes('criada com sucesso') ? 'text-emerald-400 font-semibold' :
                        line.includes('AVISO') ? 'text-amber-400' :
                        'text-neutral-500 dark:text-emerald-100/70'
                      }`}>
                        {line}
                      </div>
                    ))
                  )}
                </div>
              </Card>
            </div>
          )}

          {/* ── Tab: Contas Criadas ── */}
          {activeTab === 'accounts' && (
            <Card>
              <div className="flex items-center justify-between mb-3">
                <h2 className="font-bold text-sm uppercase tracking-wide text-neutral-500 dark:text-emerald-300/70">
                  Contas Criadas
                </h2>
                <button onClick={refreshAccounts}
                  className="text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 transition">
                  ↻ Atualizar
                </button>
              </div>
              {accounts.length === 0 ? (
                <div className="text-center py-10">
                  <p className="text-neutral-400 dark:text-neutral-500 text-sm">Nenhuma conta salva</p>
                  <p className="text-neutral-300 dark:text-neutral-600 text-xs mt-1">Contas criadas com sucesso aparecerão aqui</p>
                </div>
              ) : (
                <div className="overflow-x-auto max-h-[500px] overflow-y-auto">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-white dark:bg-[#0a1a12]">
                      <tr className="text-left text-neutral-400 border-b border-neutral-200 dark:border-neutral-800 text-xs uppercase tracking-wide">
                        <th className="px-2 py-1.5 font-medium">Nome</th>
                        <th className="px-2 py-1.5 font-medium">E-mail / Login</th>
                        <th className="px-2 py-1.5 font-medium">Senha</th>
                        <th className="px-2 py-1.5 font-medium">Browser</th>
                        <th className="px-2 py-1.5 font-medium">OS</th>
                        <th className="px-2 py-1.5 font-medium">Data</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...accounts].reverse().map((a, i) => (
                        <tr key={i} className="border-b border-neutral-100 dark:border-neutral-800/60 hover:bg-neutral-50 dark:hover:bg-neutral-800/40">
                          <td className="px-2 py-2 font-medium">{a.first_name} {a.last_name}</td>
                          <td className="px-2 py-2 font-mono text-xs">{a.email || a.phone || '—'}</td>
                          <td className="px-2 py-2">
                            <CopyablePassword password={a.password} />
                          </td>
                          <td className="px-2 py-2 text-xs">
                            <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold
                              ${a.browser_type === 'chromium'
                                ? 'bg-blue-50 text-blue-600 dark:bg-blue-950/30 dark:text-blue-300'
                                : 'bg-purple-50 text-purple-600 dark:bg-purple-950/30 dark:text-purple-300'}`}>
                              {a.browser_type === 'chromium' ? '🌐 Chromium' : '🛡️ IxBrowser'}
                            </span>
                          </td>
                          <td className="px-2 py-2 text-xs">
                            {a.os_type === 'android' ? '📱' : a.os_type === 'ios' ? '🍎' : '🖥️'}
                          </td>
                          <td className="px-2 py-2 text-xs text-neutral-400">{a.created_at || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </>
  )
}

/* ── Password with show/copy ──────────────────────────────── */

function CopyablePassword({ password }) {
  const [show, setShow] = useState(false)
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(password || '')
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {}
  }

  if (!password) return <span className="text-neutral-400">—</span>

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="font-mono text-xs">
        {show ? password : '••••••••'}
      </span>
      <button type="button" onClick={() => setShow(!show)}
        className="text-[10px] text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 transition"
        title={show ? 'Esconder' : 'Mostrar'}>
        {show ? '🙈' : '👁️'}
      </button>
      <button type="button" onClick={handleCopy}
        className="text-[10px] text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 transition"
        title="Copiar">
        {copied ? '✓' : '📋'}
      </button>
    </span>
  )
}

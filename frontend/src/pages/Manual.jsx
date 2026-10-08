import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Play, FileText, Lock, Zap, Phone, Settings } from "lucide-react"
import { api } from "../api.js"
import { Card, CopyButton, PageHead, btnPrimary, inputCls } from "../components/ui.jsx"
import { CnpjCard } from "../components/CnpjCard.jsx"
import RunsPanel from "../components/RunsPanel.jsx"

const FIELDS = [
  { key: 'cnpj', label: 'CNPJ', ph: '00.000.000/0000-00' },
  { key: 'email', label: 'E-mail / telefone do Facebook', ph: 'login@exemplo.com' },
  { key: 'password', label: 'Senha', type: 'password' },
  { key: 'totp_seed', label: 'Seed do 2FA (TOTP)', ph: 'BASE32…' },
]
// Campos da conta (sem o CNPJ) — só aparecem quando NÃO se importa do estoque.
const ACCOUNT_FIELDS = FIELDS.filter((f) => f.key !== 'cnpj')

// Telefone sms24h no padrão BR (XX) XXXXX-XXXX; tira o 55 do país se vier.
function fmtPhone(raw) {
  const d = (raw || '').replace(/\D/g, '')
  if (d.length >= 11) return `(${d.slice(-11, -9)}) ${d.slice(-9, -4)}-${d.slice(-4)}`
  if (d.length === 10) return `(${d.slice(0, 2)}) ${d.slice(2, 6)}-${d.slice(6)}`
  return raw || ''
}

export default function Manual() {
  const [form, setForm] = useState({ cnpj: '', email: '', password: '', totp_seed: '' })
  const [msg, setMsg] = useState(null)
  // Correções do operador aos dados da Receita (número, telefone…). Sobem do
  // CnpjCard via onChange e vão no payload do run como `overrides`.
  const [overrides, setOverrides] = useState(null)
  // Usar proxy do pool (Webshare). Desligar quando roteando pelo celular/4G —
  // a run sai pelo IP direto (sem pool; evita o 402 do Webshare sem banda).
  const [useProxy, setUseProxy] = useState(true)
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  // Estoque de contas (pool) p/ importar sem digitar. accountEmail setado =
  // modo import: usa email/senha/2FA salvos (o operador não re-digita).
  const [stock, setStock] = useState([])
  const [accountEmail, setAccountEmail] = useState('')
  useEffect(() => {
    api.pools()
      .then((p) => setStock((p.accounts || []).filter((a) => a.status !== 'used')))
      .catch(() => {})
  }, [])

  // Prévia automática: quando o CNPJ chega a 14 dígitos, consulta a BrasilAPI
  // (debounce p/ não disparar a cada tecla; o backend cacheia). Some se ficar
  // incompleto. Assim dá pra conferir/corrigir os dados antes do run.
  const cnpjDigits = form.cnpj.replace(/\D/g, '')
  const [previewCnpj, setPreviewCnpj] = useState(null)
  useEffect(() => {
    if (cnpjDigits.length !== 14) { setPreviewCnpj(null); setOverrides(null); return }
    const t = setTimeout(() => setPreviewCnpj(cnpjDigits), 400)
    return () => clearTimeout(t)
  }, [cnpjDigits])

  // Telefone sms24h DESTA run: alocado no worker ao iniciar (não existe no
  // momento de preencher o form) — então consultamos a run e mostramos assim
  // que o número aparecer.
  const [lastRunId, setLastRunId] = useState(null)
  const [smsPhone, setSmsPhone] = useState(null)

  const run = async () => {
    setMsg(null); setSmsPhone(null); setLastRunId(null)
    try {
      const res = await api.manualRun({
        cnpj: form.cnpj, email: form.email, password: form.password,
        totp_seed: form.totp_seed,
        account_email: accountEmail || undefined,
        overrides,
        use_proxy: useProxy,
      })
      setLastRunId(res.run_id)
      setMsg({ ok: true, text: `Run iniciada (${res.run_id.slice(0, 12)}…). Veja em Runs & Logs.` })
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  // Pronto quando tem CNPJ E (conta importada OU credenciais digitadas).
  const ready = form.cnpj.trim() && (
    accountEmail || (form.email.trim() && form.password.trim() && form.totp_seed.trim())
  )

  // Faz polling da run até o número sms24h ser alocado (até ~2min; o número
  // pode não ser alocado se o SMS estiver desabilitado).
  useEffect(() => {
    if (!lastRunId) return
    let alive = true, tries = 0
    const t = setInterval(async () => {
      tries += 1
      try {
        const r = await api.runDetail(lastRunId)
        if (alive && r && r.sms_phone) { setSmsPhone(r.sms_phone); clearInterval(t) }
      } catch { /* ignora — tenta de novo */ }
      if (tries >= 40) clearInterval(t)
    }, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [lastRunId])

  return (
    <>
      <PageHead title="Disparar Manual" subtitle="Inicie uma run com CNPJ e conta específicos" />

      <div className="grid gap-6 grid-cols-1 lg:grid-cols-3">
        {/* Formulário */}
        <Card className="lg:col-span-1 border-2 border-blue-200 dark:border-blue-900 bg-gradient-to-br from-blue-50 to-cyan-50 dark:from-blue-950/30 dark:to-cyan-950/30">
          <div className="flex items-center gap-2 mb-5">
            <Zap size={20} className="text-blue-600" />
            <h3 className="text-lg font-bold text-blue-900 dark:text-blue-100">Nova Run</h3>
          </div>

          <div className="grid gap-4">
            {/* CNPJ */}
            <div className="grid gap-2">
              <label className="text-sm block">
                <span className="flex items-center gap-1.5 text-blue-900 dark:text-blue-100 font-bold mb-2">
                  <FileText size={16} /> CNPJ
                </span>
                <input
                  type="text" placeholder="00.000.000/0000-00" value={form.cnpj}
                  onChange={(e) => set('cnpj', e.target.value)} className={`${inputCls} border-blue-200 dark:border-blue-800`}
                />
              </label>
              {previewCnpj && <CnpjCard cnpj={previewCnpj} editable onChange={setOverrides} />}
            </div>

            {/* Conta: importar OU digitar */}
            {stock.length > 0 && (
              <label className="text-sm">
                <span className="block text-blue-900 dark:text-blue-100 font-bold mb-2">Importar Conta ({stock.length})</span>
                <select value={accountEmail} onChange={(e) => setAccountEmail(e.target.value)} className={`${inputCls} border-blue-200 dark:border-blue-800`}>
                  <option value="">— digitar manualmente —</option>
                  {stock.map((a) => <option key={a.email} value={a.email}>{a.email}</option>)}
                </select>
              </label>
            )}

            {accountEmail ? (
              <div className="rounded-lg border-2 border-green-300 dark:border-green-800 bg-green-50 dark:bg-green-950/40 p-3.5">
                <span className="text-xs font-bold uppercase text-green-700 dark:text-green-300 mb-1 block">✓ Conta selecionada</span>
                <span className="font-mono font-bold text-green-900 dark:text-green-100 text-sm block mb-1.5">{accountEmail}</span>
                <span className="text-xs text-green-700 dark:text-green-300">Senha e 2FA vêm do pool. Sai do estoque ao iniciar.</span>
              </div>
            ) : (
              ACCOUNT_FIELDS.map((f) => (
                <label key={f.key} className="text-sm block">
                  <span className="block text-blue-900 dark:text-blue-100 font-bold mb-2">{f.label}</span>
                  <input
                    type={f.type || 'text'} placeholder={f.ph} value={form[f.key]}
                    onChange={(e) => set(f.key, e.target.value)} className={`${inputCls} border-blue-200 dark:border-blue-800`}
                  />
                </label>
              ))
            )}

            {/* Proxy toggle */}
            <label className="flex items-center gap-3 text-sm select-none cursor-pointer bg-white dark:bg-neutral-800 p-3 rounded-lg border border-blue-200 dark:border-blue-800">
              <input
                type="checkbox" checked={useProxy}
                onChange={(e) => setUseProxy(e.target.checked)}
                className="h-4 w-4 accent-blue-600"
              />
              <div>
                <span className="text-blue-900 dark:text-blue-100 font-bold block">Usar Proxy (Webshare)</span>
                <span className="text-xs text-blue-700 dark:text-blue-300">Desligue se estiver em 4G/VPN</span>
              </div>
            </label>

            {/* Botão iniciar */}
            <button onClick={run} disabled={!ready} className={`${btnPrimary} px-6 py-3.5 font-bold flex items-center justify-center gap-2`}>
              <Play size={18} />
              {ready ? '🚀 Iniciar Run' : '⏳ Preencha os campos'}
            </button>

            {/* Mensagens */}
            {msg && <p className={`text-sm p-3 rounded-lg font-medium ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/40' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>{msg.text}</p>}

            {/* SMS */}
            {lastRunId && (
              <div className={`rounded-lg border-2 p-3.5 ${smsPhone ? 'border-green-300 dark:border-green-800 bg-green-50 dark:bg-green-950/40' : 'border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40'}`}>
                <span className="flex items-center gap-1.5 text-xs font-bold uppercase mb-2">
                  <Phone size={14} />
                  {smsPhone ? '✓ SMS24h' : '⏳ Aguardando SMS'}
                </span>
                {smsPhone ? (
                  <span className="text-sm font-mono font-bold flex items-center gap-2 text-green-900 dark:text-green-100">
                    {fmtPhone(smsPhone)}
                    <CopyButton text={smsPhone} label="copiar" className="text-xs text-green-600 dark:text-green-400 hover:underline shrink-0" />
                  </span>
                ) : (
                  <span className="text-sm text-amber-700 dark:text-amber-300">alocando número…</span>
                )}
              </div>
            )}

            <p className="text-xs text-blue-700 dark:text-blue-300 bg-blue-100 dark:bg-blue-900/20 p-2.5 rounded-lg">
              <b>⚙️ Antes de começar:</b> Configure <Link to="/settings" className="underline font-bold hover:text-blue-900 dark:hover:text-blue-100">Credenciais</Link>
            </p>
          </div>
        </Card>

        {/* Logs ao vivo */}
        <div className="lg:col-span-2">
          <RunsPanel variant="side" focusRunId={lastRunId} />
        </div>
      </div>
    </>
  )
}

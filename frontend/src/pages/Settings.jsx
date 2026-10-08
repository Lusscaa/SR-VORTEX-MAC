import { useEffect, useState } from "react"
import { Key, Download, Cloud, AlertCircle, CheckCircle, Zap, RefreshCw } from "lucide-react"
import { api } from "../api.js"
import { Badge, Card, PageHead, btnPrimary, inputCls } from "../components/ui.jsx"

const FIELDS = [
  { key: 'adspower_api_key', label: 'AdsPower API Token', secret: true, ph: 'cole o token' },
  // Hospedagem dos sites por CNPJ agora é Netlify (ver a seção "Domínios
  // Netlify" abaixo): domínio + token + limite, cadastrados em runtime.
]

export default function Settings() {
  const [current, setCurrent] = useState({})
  const [form, setForm] = useState({})
  const [msg, setMsg] = useState(null)
  const [domains, setDomains] = useState([])
  const [cfDomains, setCfDomains] = useState([])
  const [provider, setProvider] = useState('netlify')

  const load = () => {
    api.settings().then((s) => { setCurrent(s); setForm({}) }).catch(() => {})
    api.netlifyDomains().then(setDomains).catch(() => {})
    api.cloudflareDomains().then(setCfDomains).catch(() => {})
    api.hostingProvider().then((r) => setProvider(r.provider)).catch(() => {})
  }
  useEffect(() => { load() }, [])

  const save = async () => {
    setMsg(null)
    try {
      const payload = Object.fromEntries(Object.entries(form).filter(([, v]) => v && v.trim()))
      await api.saveSettings(payload)
      setMsg({ ok: true, text: 'Credenciais salvas.' })
      load()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  return (
    <>
      <PageHead title="Credenciais" subtitle="Provedor de hospedagem e domínios (DNS) dos sites" />

      <HostingProviderToggle provider={provider} onChange={setProvider} onRefresh={load} />
      <CloudflareDomains domains={cfDomains} onRefresh={load} />
      <NetlifyDomains domains={domains} onRefresh={load} />
      <AccountsExport />
    </>
  )
}

// ---------------------------------------------------------------------------
// Reiniciar o serviço — recarrega o .env (ex.: nova sessão do SMS
// NUMERO_VIRTUAL_CUSTOMER_SESSION, que expira ~a cada 7 dias). Fecha e sobe o
// backend sozinho; runs/BMs/pools ficam salvos no banco. O painel espera o
// backend voltar e recarrega a tela.
// ---------------------------------------------------------------------------
function RestartService() {
  const [confirm, setConfirm] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [msg, setMsg] = useState(null)

  // Espera o backend voltar depois do restart. Durante a queda o fetch falha
  // (conexão recusada) — engolimos o erro e tentamos de novo até ~60s.
  const waitForBackend = async () => {
    const deadline = Date.now() + 60000
    await new Promise((r) => setTimeout(r, 2500))  // dá tempo do processo cair
    while (Date.now() < deadline) {
      try {
        await api.health()
        return true
      } catch {
        await new Promise((r) => setTimeout(r, 1500))
      }
    }
    return false
  }

  const restart = async () => {
    setConfirm(false)
    setMsg(null)
    setRestarting(true)
    try {
      // A resposta chega antes de o processo cair (o backend atrasa 0.6s).
      try {
        await api.restartBackend()
      } catch { /* conexão pode cair antes de responder — segue esperando */ }
      const back = await waitForBackend()
      if (back) {
        setMsg({ ok: true, text: '✓ Serviço reiniciado — .env recarregado. Atualizando o painel…' })
        setTimeout(() => window.location.reload(), 1000)
      } else {
        setMsg({ ok: false, text: 'O serviço não voltou em 60s. Confira a janela do backend (INICIAR_BACKEND.bat).' })
        setRestarting(false)
      }
    } catch (e) {
      setMsg({ ok: false, text: e.message })
      setRestarting(false)
    }
  }

  return (
    <Card className="max-w-2xl mb-6 border-2 border-amber-200 dark:border-amber-900 bg-gradient-to-br from-amber-50 to-orange-50 dark:from-amber-950/30 dark:to-orange-950/30">
      <div className="flex items-center gap-2 mb-3">
        <RefreshCw size={20} className={`text-amber-600 ${restarting ? 'animate-spin' : ''}`} />
        <h3 className="text-lg font-bold text-amber-900 dark:text-amber-100">Reiniciar serviço</h3>
      </div>
      <p className="text-sm text-amber-800 dark:text-amber-200 mb-4 leading-relaxed">
        Reinicia o backend para <b>recarregar o <span className="font-mono">.env</span></b> — use depois de trocar a
        sessão do SMS (<span className="font-mono">NUMERO_VIRTUAL_CUSTOMER_SESSION</span>) ou qualquer credencial.
        Fecha e sobe tudo sozinho; runs, BMs e estoque ficam salvos.
        <br />
        <span className="text-amber-700 dark:text-amber-300 font-medium">⚠️ Qualquer run em andamento (navegador aberto) será interrompida.</span>
      </p>
      {!confirm ? (
        <button
          onClick={() => { setMsg(null); setConfirm(true) }}
          disabled={restarting}
          className="rounded-lg bg-amber-500 hover:bg-amber-600 text-white px-6 py-3 text-sm font-semibold disabled:opacity-50 flex items-center gap-2 transition"
        >
          <RefreshCw size={16} className={restarting ? 'animate-spin' : ''} />
          {restarting ? 'Reiniciando…' : '🔄 Reiniciar serviço'}
        </button>
      ) : (
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-sm font-bold text-amber-900 dark:text-amber-100">Reiniciar agora?</span>
          <button
            onClick={restart}
            className="rounded-lg bg-amber-600 hover:bg-amber-700 text-white px-5 py-2.5 text-sm font-bold transition"
          >
            Sim, reiniciar
          </button>
          <button
            onClick={() => setConfirm(false)}
            className="rounded-lg border-2 border-amber-400 dark:border-amber-700 text-amber-700 dark:text-amber-300 px-5 py-2.5 text-sm font-semibold hover:bg-amber-100 dark:hover:bg-amber-900/40 transition"
          >
            Cancelar
          </button>
        </div>
      )}
      {msg && (
        <p className={`text-sm mt-4 p-3 rounded-lg font-medium ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/40' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>
          {msg.text}
        </p>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Exportar contas do pool
// ---------------------------------------------------------------------------
function AccountsExport() {
  const [exporting, setExporting] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [msg, setMsg] = useState(null)

  // Busca o .txt (uma conta por linha) do backend — já vem pronto de lá
  // (email:senha:2fa:email_address:email_password), então aqui só decide o
  // que fazer com o texto (copiar ou baixar).
  const fetchExportText = async () => {
    const token = localStorage.getItem('panel_token')
    const response = await fetch('/api/pools/accounts/export', {
      headers: { Authorization: `Bearer ${token}` }
    })
    if (!response.ok) throw new Error(response.statusText)
    return response.text()
  }

  const exportAccounts = async () => {
    setMsg(null)
    setExporting(true)
    try {
      const text = await fetchExportText()
      const lines = text.trim().split('\n').filter(l => l.length > 0)
      await navigator.clipboard.writeText(text)
      setMsg({
        ok: true,
        text: `✓ ${lines.length} contas copiadas para a clipboard (email:senha:2fa:email_address:email_password).`
      })
    } catch (e) {
      setMsg({ ok: false, text: e.message })
    } finally {
      setExporting(false)
    }
  }

  // Baixa um .txt de verdade — uma conta por linha, sem JSON, sem aspas.
  const downloadAccounts = async () => {
    setMsg(null)
    setDownloading(true)
    try {
      const text = await fetchExportText()
      const lines = text.trim().split('\n').filter(l => l.length > 0)
      if (!lines.length) {
        setMsg({ ok: false, text: 'Nenhuma conta disponível no estoque pra exportar.' })
        return
      }
      const stamp = new Date().toISOString().slice(0, 10)
      const blob = new Blob([text.trim() + '\n'], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `contas_disponiveis_${stamp}.txt`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setMsg({
        ok: true,
        text: `✓ ${lines.length} contas baixadas em contas_disponiveis_${stamp}.txt (uma por linha).`
      })
    } catch (e) {
      setMsg({ ok: false, text: e.message })
    } finally {
      setDownloading(false)
    }
  }

  return (
    <Card className="max-w-2xl mt-6 border-2 border-green-200 dark:border-green-900 bg-gradient-to-br from-green-50 to-emerald-50 dark:from-green-950/30 dark:to-emerald-950/30">
      <div className="flex items-center gap-2 mb-4">
        <Download size={20} className="text-green-600" />
        <h3 className="text-lg font-bold text-green-900 dark:text-green-100">
          Exportar Contas
        </h3>
      </div>
      <p className="text-sm text-green-800 dark:text-green-200 mb-4 leading-relaxed">
        Contas disponíveis do estoque, uma por linha (email:senha:2fa:email_address:email_password). Copie para a clipboard ou baixe o arquivo .txt — perfeito para backup ou migração.
      </p>
      <div className="flex gap-2">
        <button
          onClick={exportAccounts}
          disabled={exporting || downloading}
          className={`${btnPrimary} px-6 py-3 text-sm font-semibold flex-1`}
        >
          {exporting ? '⏳ Copiando…' : '📋 Copiar todas as contas'}
        </button>
        <button
          onClick={downloadAccounts}
          disabled={exporting || downloading}
          className="rounded-lg border-2 border-green-400 dark:border-green-700 text-green-700 dark:text-green-300 px-6 py-3 text-sm font-semibold flex-1 disabled:opacity-50 hover:bg-green-100 dark:hover:bg-green-900/40 transition"
        >
          {downloading ? '⏳ Baixando…' : '⬇️ Baixar arquivo (.txt)'}
        </button>
      </div>
      {msg && (
        <p className={`text-sm mt-4 p-3 rounded-lg font-medium ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-200 dark:bg-green-900/50' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>
          {msg.text}
        </p>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Domínios Netlify (host atual) — cada CNPJ vira um subdomínio
// ---------------------------------------------------------------------------
function NetlifyDomains({ domains, onRefresh }) {
  const [newDomain, setNewDomain] = useState('')
  const [newToken, setNewToken] = useState('')
  const [newCap, setNewCap] = useState('10')
  const [adding, setAdding] = useState(false)
  const [msg, setMsg] = useState(null)
  const [lastNs, setLastNs] = useState(null)  // {domain, nameservers, propagated}

  const add = async () => {
    setMsg(null); setLastNs(null)
    if (!newDomain.trim()) { setMsg({ ok: false, text: 'Informe o domínio (ex.: meudominio.com.br).' }); return }
    if (!newToken.trim()) { setMsg({ ok: false, text: 'Cole o token da Netlify.' }); return }
    setAdding(true)
    try {
      const res = await api.addNetlifyDomain(newDomain.trim(), newToken.trim(), parseInt(newCap, 10) || 10)
      setLastNs({ domain: res.domain, nameservers: res.nameservers || [], propagated: res.propagated })
      setNewDomain(''); setNewToken(''); setNewCap('10')
      setMsg({ ok: true, text: res.propagated
        ? 'Domínio adicionado e nameservers já propagados! Pronto para uso.'
        : 'Domínio adicionado. Configure os nameservers abaixo na Hostinger.' })
      onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setAdding(false) }
  }

  const remove = async (domain) => {
    setMsg(null)
    try { await api.deleteNetlifyDomain(domain); onRefresh() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  const reset = async (domain) => {
    setMsg(null)
    try { await api.resetNetlifyDomain(domain); setMsg({ ok: true, text: 'Contador zerado.' }); onRefresh() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  const setQuota = async (domain, remaining, cap) => {
    setMsg(null)
    try {
      await api.setNetlifyDomainQuota(domain, remaining, cap)
      setMsg({ ok: true, text: `${domain}: ${remaining} vaga(s) restante(s) de ${cap}. A contagem continua.` })
      onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  const checkStatus = async (domain) => {
    setMsg(null)
    try {
      const r = await api.netlifyDomainStatus(domain)
      setMsg({ ok: r.propagated, text: r.propagated
        ? `✓ ${domain}: nameservers propagados! Domínio liberado.`
        : `⏳ ${domain}: nameservers ainda não propagaram. Aguarde (pode levar até algumas horas).` })
      onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  return (
    <Card className="max-w-2xl mt-6 border-2 border-purple-200 dark:border-purple-900 bg-gradient-to-br from-purple-50 to-pink-50 dark:from-purple-950/30 dark:to-pink-950/30">
      <div className="flex items-center gap-2 mb-5">
        <Cloud size={20} className="text-purple-600" />
        <h3 className="text-lg font-bold text-purple-900 dark:text-purple-100">
          Domínios Netlify
        </h3>
      </div>

      <p className="text-sm text-purple-800 dark:text-purple-200 mb-5 leading-relaxed bg-purple-100 dark:bg-purple-900/20 p-3 rounded-lg">
        <b>🔄 Como funciona:</b> cada CNPJ vira um subdomínio do seu domínio (ex.: <span className="font-mono">empresa.seudominio.com</span>). O bot usa o primeiro domínio com vagas e nameservers propagados; quando enche o limite, passa para o próximo. Ao adicionar, mostramos os <b>nameservers</b> para você colocar na Hostinger.
      </p>

      {/* Lista de domínios */}
      {domains.length === 0 ? (
        <div className="text-sm text-purple-600 dark:text-purple-300 mb-5 p-4 bg-purple-100 dark:bg-purple-900/20 rounded-lg border-l-4 border-purple-400">
          📭 Nenhum domínio adicionado ainda.
        </div>
      ) : (
        <div className="flex flex-col gap-3 mb-6">
          {domains.map((d, i) => (
            <NetlifyDomainRow
              key={d.domain}
              domain={d}
              isActive={i === domains.findIndex((x) => x.remaining > 0 && x.dns_ok)}
              onReset={() => reset(d.domain)}
              onDelete={() => remove(d.domain)}
              onCheck={() => checkStatus(d.domain)}
              onSaveQuota={(remaining, cap) => setQuota(d.domain, remaining, cap)}
            />
          ))}
        </div>
      )}

      {/* Nameservers do último domínio adicionado */}
      {lastNs && lastNs.nameservers.length > 0 && (
        <div className="mb-6 p-4 rounded-lg border-2 border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30">
          <p className="text-sm font-bold text-amber-900 dark:text-amber-100 mb-2">
            🌐 Configure estes nameservers na Hostinger para <span className="font-mono">{lastNs.domain}</span>:
          </p>
          <div className="flex flex-col gap-1.5">
            {lastNs.nameservers.map((ns) => (
              <code key={ns} className="text-xs bg-white dark:bg-neutral-800 px-3 py-2 rounded border border-amber-200 dark:border-amber-800 font-mono text-neutral-800 dark:text-neutral-200 select-all">
                {ns}
              </code>
            ))}
          </div>
          <p className="text-xs text-amber-700 dark:text-amber-300 mt-2 leading-relaxed">
            Na Hostinger: <b>Domínios → seu domínio → DNS / Nameservers → Alterar nameservers → Usar personalizados</b>, cole os acima e salve. A propagação leva de minutos a algumas horas. Depois clique em <b>🔄 Verificar</b> no domínio.
          </p>
        </div>
      )}

      {/* Formulário de adição */}
      <div className="border-t-2 border-purple-200 dark:border-purple-800 pt-6 grid gap-4">
        <div className="flex items-center gap-2 mb-2">
          <Zap size={18} className="text-purple-600" />
          <p className="text-sm font-bold text-purple-900 dark:text-purple-100">Adicionar domínio</p>
        </div>
        <input
          type="text"
          placeholder="🌐 Domínio (ex.: meudominio.com.br)"
          value={newDomain}
          onChange={(e) => setNewDomain(e.target.value)}
          className={`${inputCls} border-purple-200 dark:border-purple-800`}
        />
        <input
          type="password"
          placeholder="🔑 Token da Netlify (Personal Access Token)"
          value={newToken}
          onChange={(e) => setNewToken(e.target.value)}
          className={`${inputCls} border-purple-200 dark:border-purple-800`}
        />
        <p className="text-xs text-purple-700 dark:text-purple-300 bg-purple-50 dark:bg-purple-900/20 p-2.5 rounded">
          O token você gera em <span className="font-mono">app.netlify.com → User settings → Applications → Personal access tokens</span>.
        </p>
        <div className="flex items-center gap-3 bg-white dark:bg-neutral-800 p-3 rounded-lg border border-purple-200 dark:border-purple-800">
          <label className="text-sm font-medium text-purple-900 dark:text-purple-100">📊 Limite de sites (CNPJs) neste domínio:</label>
          <input
            type="number"
            min="1"
            max="99999"
            value={newCap}
            onChange={(e) => setNewCap(e.target.value)}
            className={`${inputCls} w-24 text-center border-purple-200 dark:border-purple-800`}
          />
        </div>
        <button
          onClick={add}
          disabled={adding}
          className={`${btnPrimary} px-6 py-3 text-sm font-semibold w-full`}
        >
          {adding ? '⏳ Criando zona DNS…' : '➕ Adicionar domínio'}
        </button>
        {msg && (
          <p className={`text-sm p-3 rounded-lg font-medium ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/40' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>
            {msg.text}
          </p>
        )}
      </div>
    </Card>
  )
}

function DomainQuotaEditor({ remaining, cap, sitesUsed, onSave }) {
  const [rem, setRem] = useState(String(remaining ?? 0))
  const [lim, setLim] = useState(String(cap ?? 1))
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    setRem(String(remaining ?? 0))
    setLim(String(cap ?? 1))
  }, [remaining, cap])
  const remN = Math.max(0, parseInt(rem, 10) || 0)
  const capN = Math.max(1, parseInt(lim, 10) || 1)
  const dirty = remN !== remaining || capN !== cap
  const save = async () => {
    if (!dirty) return
    setSaving(true)
    try { await onSave(remN, capN) }
    finally { setSaving(false) }
  }
  return (
    <div className="flex flex-wrap items-end gap-2 mb-2">
      <label className="grid gap-0.5">
        <span className="text-[10px] uppercase tracking-wide text-neutral-500 font-semibold">Ainda disponíveis</span>
        <input
          type="number" min="0" max="99999" value={rem}
          onChange={(e) => setRem(e.target.value)}
          className={`${inputCls} w-20 text-center text-lg font-bold tabular-nums py-1`}
        />
      </label>
      <label className="grid gap-0.5">
        <span className="text-[10px] uppercase tracking-wide text-neutral-500 font-semibold">Limite</span>
        <input
          type="number" min="1" max="99999" value={lim}
          onChange={(e) => setLim(e.target.value)}
          className={`${inputCls} w-20 text-center py-1`}
        />
      </label>
      <button
        type="button" onClick={save} disabled={!dirty || saving}
        className={`${btnPrimary} px-3 py-1.5 text-xs disabled:opacity-40`}
      >
        {saving ? 'Salvando…' : 'Salvar cota'}
      </button>
      <span className="text-xs text-neutral-500 pb-1">
        {sitesUsed} usada{sitesUsed === 1 ? '' : 's'} · o bot continua somando +1 a cada site novo
      </span>
    </div>
  )
}

function NetlifyDomainRow({ domain, isActive, onReset, onDelete, onCheck, onSaveQuota }) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const [showNs, setShowNs] = useState(false)

  const { domain: name, api_token_masked, cap, sites_used, remaining, dns_ok, nameservers } = domain
  const pct = Math.min(100, Math.round((sites_used / Math.max(1, cap)) * 100))
  const tone = remaining <= 0 ? 'red' : remaining <= 2 ? 'amber' : 'lime'
  const barCls = tone === 'red' ? 'bg-red-500' : tone === 'amber' ? 'bg-amber-500' : 'bg-lime-500'

  const bgColor = !dns_ok ? 'from-amber-50 to-orange-50 dark:from-amber-950/30 dark:to-orange-950/30 border-amber-200 dark:border-amber-900'
                  : remaining <= 0 ? 'from-red-50 to-rose-50 dark:from-red-950/30 dark:to-rose-950/30 border-red-200 dark:border-red-900'
                  : 'from-green-50 to-emerald-50 dark:from-green-950/30 dark:to-emerald-950/30 border-green-200 dark:border-green-900'

  return (
    <div className={`rounded-lg border-2 bg-gradient-to-r ${bgColor} p-4 shadow-sm hover:shadow-md transition-shadow`}>
      <div className="flex items-center justify-between mb-3 gap-2">
        <div className="flex items-center gap-2.5 min-w-0 flex-wrap">
          {!dns_ok && (
            <span className="shrink-0 text-xs font-bold uppercase tracking-wide bg-amber-200 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 px-2 py-1 rounded-full">
              ⏳ aguardando NS
            </span>
          )}
          {dns_ok && isActive && remaining > 0 && (
            <span className="shrink-0 text-xs font-bold uppercase tracking-wide bg-green-200 dark:bg-green-900/60 text-green-700 dark:text-green-300 px-2 py-1 rounded-full">
              🟢 ativo
            </span>
          )}
          {dns_ok && remaining <= 0 && (
            <span className="shrink-0 text-xs font-bold uppercase tracking-wide bg-red-200 dark:bg-red-900/60 text-red-700 dark:text-red-300 px-2 py-1 rounded-full">
              🔴 cheio
            </span>
          )}
          <span className="text-sm font-mono text-neutral-700 dark:text-neutral-200 truncate font-bold" title={name}>
            🌐 {name}
          </span>
          <span className="text-xs text-neutral-600 dark:text-neutral-400 shrink-0">{api_token_masked}</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0 text-xs">
          <button onClick={onCheck} className="text-blue-600 dark:text-blue-400 hover:underline font-medium">🔄 Verificar</button>
          {confirmReset ? (
            <>
              <span className="text-neutral-600 dark:text-neutral-300">Zerar?</span>
              <button onClick={() => { onReset(); setConfirmReset(false) }} className="text-green-600 dark:text-green-400 font-bold hover:underline">Sim</button>
              <button onClick={() => setConfirmReset(false)} className="text-neutral-500 hover:underline">não</button>
            </>
          ) : (
            <button onClick={() => setConfirmReset(true)} className="text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300 underline">Zerar</button>
          )}
          {confirmDelete ? (
            <>
              <span className="text-neutral-600 dark:text-neutral-300">Remover?</span>
              <button onClick={() => { onDelete(); setConfirmDelete(false) }} className="text-red-600 dark:text-red-400 font-bold hover:underline">Sim</button>
              <button onClick={() => setConfirmDelete(false)} className="text-neutral-500 hover:underline">não</button>
            </>
          ) : (
            <button onClick={() => setConfirmDelete(true)} className="text-neutral-500 hover:text-red-600 dark:hover:text-red-400 font-bold text-lg" title="Remover">✕</button>
          )}
        </div>
      </div>

      <DomainQuotaEditor
        remaining={remaining} cap={cap} sitesUsed={sites_used}
        onSave={onSaveQuota}
      />
      <div className="h-2.5 rounded-full bg-neutral-300 dark:bg-neutral-700 overflow-hidden shadow-sm">
        <div className={`h-full ${barCls} transition-[width] duration-500 rounded-full`} style={{ width: `${pct}%` }} />
      </div>

      {nameservers && nameservers.length > 0 && (
        <div className="mt-3">
          <button onClick={() => setShowNs((v) => !v)} className="text-xs text-purple-600 dark:text-purple-300 hover:underline font-medium">
            {showNs ? '▾ ocultar nameservers' : '▸ ver nameservers (Hostinger)'}
          </button>
          {showNs && (
            <div className="flex flex-col gap-1.5 mt-2">
              {nameservers.map((ns) => (
                <code key={ns} className="text-xs bg-white dark:bg-neutral-800 px-3 py-1.5 rounded border border-neutral-200 dark:border-neutral-700 font-mono text-neutral-800 dark:text-neutral-200 select-all">{ns}</code>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Toggle do provedor de hospedagem (Netlify padrão | Cloudflare — teste)
// ---------------------------------------------------------------------------
function HostingProviderToggle({ provider, onChange, onRefresh }) {
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)

  const pick = async (p) => {
    if (p === provider) return
    setSaving(true); setMsg(null)
    try {
      const r = await api.setHostingProvider(p)
      onChange(r.provider)
      setMsg({ ok: true, text: r.provider === 'cloudflare'
        ? 'Provedor trocado para Cloudflare. As próximas runs usarão o domínio Cloudflare.'
        : 'Provedor de volta para Netlify (padrão).' })
      onRefresh && onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setSaving(false) }
  }

  const btn = (p, label, emoji) => (
    <button
      onClick={() => pick(p)}
      disabled={saving}
      className={`px-5 py-3 rounded-lg text-sm font-bold border-2 transition-all ${
        provider === p
          ? 'bg-orange-500 text-white border-orange-500 shadow-md'
          : 'bg-white dark:bg-neutral-800 text-neutral-700 dark:text-neutral-200 border-neutral-300 dark:border-neutral-700 hover:border-orange-400'
      }`}
    >
      {emoji} {label}{provider === p ? ' ✓' : ''}
    </button>
  )

  return (
    <Card className="max-w-2xl mt-6 border-2 border-orange-200 dark:border-orange-900 bg-gradient-to-br from-orange-50 to-amber-50 dark:from-orange-950/30 dark:to-amber-950/30">
      <div className="flex items-center gap-2 mb-4">
        <Cloud size={20} className="text-orange-600" />
        <h3 className="text-lg font-bold text-orange-900 dark:text-orange-100">Provedor de hospedagem</h3>
      </div>
      <p className="text-sm text-orange-800 dark:text-orange-200 mb-4 leading-relaxed bg-orange-100 dark:bg-orange-900/20 p-3 rounded-lg">
        Escolhe quem hospeda os sites por CNPJ. <b>Netlify</b> é o padrão (o que já funciona). <b>Cloudflare</b> é o modo de teste — cadastre um domínio Cloudflare abaixo antes de ativar. Você pode voltar para o Netlify a qualquer momento.
      </p>
      <div className="flex gap-3">
        {btn('netlify', 'Netlify', '🟣')}
        {btn('cloudflare', 'Cloudflare', '🟠')}
      </div>
      {msg && (
        <p className={`text-sm p-3 rounded-lg font-medium mt-4 ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/40' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>
          {msg.text}
        </p>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Domínios Cloudflare (host alternativo; mesmo modelo de subdomínio por CNPJ)
// ---------------------------------------------------------------------------
function CloudflareDomains({ domains, onRefresh }) {
  const [newDomain, setNewDomain] = useState('')
  const [newAccount, setNewAccount] = useState('')
  const [newToken, setNewToken] = useState('')
  const [newCap, setNewCap] = useState('100')
  const [adding, setAdding] = useState(false)
  const [msg, setMsg] = useState(null)
  const [lastNs, setLastNs] = useState(null)

  const add = async () => {
    setMsg(null); setLastNs(null)
    if (!newDomain.trim()) { setMsg({ ok: false, text: 'Informe o domínio (ex.: meudominio.com.br).' }); return }
    if (!newAccount.trim()) { setMsg({ ok: false, text: 'Informe o Account ID da Cloudflare.' }); return }
    if (!newToken.trim()) { setMsg({ ok: false, text: 'Cole o API Token da Cloudflare.' }); return }
    setAdding(true)
    try {
      const res = await api.addCloudflareDomain(newDomain.trim(), newAccount.trim(), newToken.trim(), parseInt(newCap, 10) || 100)
      setLastNs({ domain: res.domain, nameservers: res.nameservers || [], propagated: res.propagated })
      setNewDomain(''); setNewAccount(''); setNewToken(''); setNewCap('100')
      setMsg({ ok: true, text: res.propagated
        ? 'Domínio adicionado e nameservers já propagados! Pronto para uso.'
        : 'Domínio adicionado. Configure os nameservers abaixo na Hostinger.' })
      onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setAdding(false) }
  }

  const remove = async (domain) => {
    setMsg(null)
    try { await api.deleteCloudflareDomain(domain); onRefresh() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  const reset = async (domain) => {
    setMsg(null)
    try { await api.resetCloudflareDomain(domain); setMsg({ ok: true, text: 'Contador zerado.' }); onRefresh() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  const setQuota = async (domain, remaining, cap) => {
    setMsg(null)
    try {
      await api.setCloudflareDomainQuota(domain, remaining, cap)
      setMsg({ ok: true, text: `${domain}: ${remaining} vaga(s) restante(s) de ${cap}. A contagem continua.` })
      onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  const checkStatus = async (domain) => {
    setMsg(null)
    try {
      const r = await api.cloudflareDomainStatus(domain)
      setMsg({ ok: r.propagated, text: r.propagated
        ? `✓ ${domain}: zona ATIVA na Cloudflare! Domínio liberado.`
        : `⏳ ${domain}: zona ainda não ativa (status: ${r.status || '—'}). Confira os nameservers na Hostinger e aguarde a propagação.` })
      onRefresh()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  return (
    <Card className="max-w-2xl mt-6 border-2 border-orange-200 dark:border-orange-900 bg-gradient-to-br from-orange-50 to-yellow-50 dark:from-orange-950/30 dark:to-yellow-950/30">
      <div className="flex items-center gap-2 mb-5">
        <Cloud size={20} className="text-orange-600" />
        <h3 className="text-lg font-bold text-orange-900 dark:text-orange-100">Domínios Cloudflare (teste)</h3>
      </div>

      <p className="text-sm text-orange-800 dark:text-orange-200 mb-5 leading-relaxed bg-orange-100 dark:bg-orange-900/20 p-3 rounded-lg">
        <b>🔄 Como funciona:</b> cada CNPJ vira um subdomínio do seu domínio (ex.: <span className="font-mono">empresa.seudominio.com</span>), servido por <b>1 Worker</b> na Cloudflare — sem limite prático de subdomínios e no plano grátis. Cadastre com <b>Account ID + API Token</b>; nós criamos a zona e mostramos os <b>nameservers</b> para você pôr na Hostinger. Ative no <b>Provedor de hospedagem</b> acima.
      </p>

      {domains.length === 0 ? (
        <div className="text-sm text-orange-600 dark:text-orange-300 mb-5 p-4 bg-orange-100 dark:bg-orange-900/20 rounded-lg border-l-4 border-orange-400">
          📭 Nenhum domínio Cloudflare adicionado ainda.
        </div>
      ) : (
        <div className="flex flex-col gap-3 mb-6">
          {domains.map((d, i) => (
            <CloudflareDomainRow
              key={d.domain}
              domain={d}
              isActive={i === domains.findIndex((x) => x.remaining > 0 && x.dns_ok)}
              onReset={() => reset(d.domain)}
              onDelete={() => remove(d.domain)}
              onCheck={() => checkStatus(d.domain)}
              onSaveQuota={(remaining, cap) => setQuota(d.domain, remaining, cap)}
            />
          ))}
        </div>
      )}

      {lastNs && lastNs.nameservers.length > 0 && (
        <div className="mb-6 p-4 rounded-lg border-2 border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30">
          <p className="text-sm font-bold text-amber-900 dark:text-amber-100 mb-2">
            🌐 Configure estes nameservers na Hostinger para <span className="font-mono">{lastNs.domain}</span>:
          </p>
          <div className="flex flex-col gap-1.5">
            {lastNs.nameservers.map((ns) => (
              <code key={ns} className="text-xs bg-white dark:bg-neutral-800 px-3 py-2 rounded border border-amber-200 dark:border-amber-800 font-mono text-neutral-800 dark:text-neutral-200 select-all">{ns}</code>
            ))}
          </div>
          <p className="text-xs text-amber-700 dark:text-amber-300 mt-2 leading-relaxed">
            Na Hostinger: <b>Domínios → seu domínio → DNS / Nameservers → Alterar nameservers → Usar personalizados</b>, cole os dois acima e salve. A propagação leva de minutos a algumas horas. Depois clique em <b>🔄 Verificar</b> no domínio.
          </p>
        </div>
      )}

      <div className="border-t-2 border-orange-200 dark:border-orange-800 pt-6 grid gap-4">
        <div className="flex items-center gap-2 mb-2">
          <Zap size={18} className="text-orange-600" />
          <p className="text-sm font-bold text-orange-900 dark:text-orange-100">Adicionar domínio Cloudflare</p>
        </div>
        <input type="text" placeholder="🌐 Domínio (ex.: meudominio.com.br)" value={newDomain} onChange={(e) => setNewDomain(e.target.value)} className={`${inputCls} border-orange-200 dark:border-orange-800`} />
        <input type="text" placeholder="🆔 Account ID da Cloudflare" value={newAccount} onChange={(e) => setNewAccount(e.target.value)} className={`${inputCls} border-orange-200 dark:border-orange-800`} />
        <input type="password" placeholder="🔑 API Token da Cloudflare" value={newToken} onChange={(e) => setNewToken(e.target.value)} className={`${inputCls} border-orange-200 dark:border-orange-800`} />
        <p className="text-xs text-orange-700 dark:text-orange-300 bg-orange-50 dark:bg-orange-900/20 p-2.5 rounded">
          O <b>Account ID</b> está no painel Cloudflare (Overview do domínio, coluna direita). O <b>API Token</b> você gera em <span className="font-mono">dash.cloudflare.com → My Profile → API Tokens → Create Token</span> com permissões de Zone, DNS e Workers.
        </p>
        <div className="flex items-center gap-3 bg-white dark:bg-neutral-800 p-3 rounded-lg border border-orange-200 dark:border-orange-800">
          <label className="text-sm font-medium text-orange-900 dark:text-orange-100">📊 Limite de CNPJs neste domínio:</label>
          <input type="number" min="1" max="99999" value={newCap} onChange={(e) => setNewCap(e.target.value)} className={`${inputCls} w-24 text-center border-orange-200 dark:border-orange-800`} />
        </div>
        <button onClick={add} disabled={adding} className={`${btnPrimary} px-6 py-3 text-sm font-semibold w-full`}>
          {adding ? '⏳ Criando zona na Cloudflare…' : '➕ Adicionar domínio'}
        </button>
        {msg && (
          <p className={`text-sm p-3 rounded-lg font-medium ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/40' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>{msg.text}</p>
        )}
      </div>
    </Card>
  )
}

function CloudflareDomainRow({ domain, isActive, onReset, onDelete, onCheck, onSaveQuota }) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const [showNs, setShowNs] = useState(false)

  const { domain: name, account_id, cap, sites_used, remaining, dns_ok, nameservers } = domain
  const pct = Math.min(100, Math.round((sites_used / Math.max(1, cap)) * 100))
  const tone = remaining <= 0 ? 'red' : remaining <= 2 ? 'amber' : 'lime'
  const barCls = tone === 'red' ? 'bg-red-500' : tone === 'amber' ? 'bg-amber-500' : 'bg-lime-500'
  const bgColor = !dns_ok ? 'from-amber-50 to-orange-50 dark:from-amber-950/30 dark:to-orange-950/30 border-amber-200 dark:border-amber-900'
                  : remaining <= 0 ? 'from-red-50 to-rose-50 dark:from-red-950/30 dark:to-rose-950/30 border-red-200 dark:border-red-900'
                  : 'from-green-50 to-emerald-50 dark:from-green-950/30 dark:to-emerald-950/30 border-green-200 dark:border-green-900'

  return (
    <div className={`rounded-lg border-2 bg-gradient-to-r ${bgColor} p-4 shadow-sm hover:shadow-md transition-shadow`}>
      <div className="flex items-center justify-between mb-3 gap-2">
        <div className="flex items-center gap-2.5 min-w-0 flex-wrap">
          {!dns_ok && (<span className="shrink-0 text-xs font-bold uppercase tracking-wide bg-amber-200 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 px-2 py-1 rounded-full">⏳ aguardando NS</span>)}
          {dns_ok && isActive && remaining > 0 && (<span className="shrink-0 text-xs font-bold uppercase tracking-wide bg-green-200 dark:bg-green-900/60 text-green-700 dark:text-green-300 px-2 py-1 rounded-full">🟢 ativo</span>)}
          {dns_ok && remaining <= 0 && (<span className="shrink-0 text-xs font-bold uppercase tracking-wide bg-red-200 dark:bg-red-900/60 text-red-700 dark:text-red-300 px-2 py-1 rounded-full">🔴 cheio</span>)}
          <span className="text-sm font-mono text-neutral-700 dark:text-neutral-200 truncate font-bold" title={name}>🌐 {name}</span>
          <span className="text-xs text-neutral-600 dark:text-neutral-400 shrink-0" title="Account ID">🆔 {account_id}</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0 text-xs">
          <button onClick={onCheck} className="text-blue-600 dark:text-blue-400 hover:underline font-medium">🔄 Verificar</button>
          {confirmReset ? (
            <>
              <span className="text-neutral-600 dark:text-neutral-300">Zerar?</span>
              <button onClick={() => { onReset(); setConfirmReset(false) }} className="text-green-600 dark:text-green-400 font-bold hover:underline">Sim</button>
              <button onClick={() => setConfirmReset(false)} className="text-neutral-500 hover:underline">não</button>
            </>
          ) : (
            <button onClick={() => setConfirmReset(true)} className="text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300 underline">Zerar</button>
          )}
          {confirmDelete ? (
            <>
              <span className="text-neutral-600 dark:text-neutral-300">Remover?</span>
              <button onClick={() => { onDelete(); setConfirmDelete(false) }} className="text-red-600 dark:text-red-400 font-bold hover:underline">Sim</button>
              <button onClick={() => setConfirmDelete(false)} className="text-neutral-500 hover:underline">não</button>
            </>
          ) : (
            <button onClick={() => setConfirmDelete(true)} className="text-neutral-500 hover:text-red-600 dark:hover:text-red-400 font-bold text-lg" title="Remover">✕</button>
          )}
        </div>
      </div>

      <DomainQuotaEditor
        remaining={remaining} cap={cap} sitesUsed={sites_used}
        onSave={onSaveQuota}
      />
      <div className="h-2.5 rounded-full bg-neutral-300 dark:bg-neutral-700 overflow-hidden shadow-sm">
        <div className={`h-full ${barCls} transition-[width] duration-500 rounded-full`} style={{ width: `${pct}%` }} />
      </div>

      {nameservers && nameservers.length > 0 && (
        <div className="mt-3">
          <button onClick={() => setShowNs((v) => !v)} className="text-xs text-orange-600 dark:text-orange-300 hover:underline font-medium">
            {showNs ? '▾ ocultar nameservers' : '▸ ver nameservers (Hostinger)'}
          </button>
          {showNs && (
            <div className="flex flex-col gap-1.5 mt-2">
              {nameservers.map((ns) => (<code key={ns} className="text-xs bg-white dark:bg-neutral-800 px-3 py-1.5 rounded border border-neutral-200 dark:border-neutral-700 font-mono text-neutral-800 dark:text-neutral-200 select-all">{ns}</code>))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

import { useEffect, useState } from "react"
import {
  KeyRound, Save, RefreshCw, Info, MessageSquare, ShieldCheck,
  Building2, MessageCircle, Chrome, Database, ExternalLink, Trash2,
  CheckCircle2, AlertTriangle, XCircle, Activity, Puzzle, Download,
} from "lucide-react"
import { api } from "../api.js"
import { Badge, Card, PageHead, btnPrimary, inputCls } from "../components/ui.jsx"

// Ícone por grupo (bate com o "id" que o backend manda em API_REGISTRY).
const GROUP_ICON = {
  sms: MessageSquare,
  captcha: ShieldCheck,
  cnpj: Building2,
  cnpj_lista: Database,
  waba: MessageCircle,
  browser: Chrome,
}

// Badge de status do grupo. `live` (opcional) vem do botão "Verificar APIs" e,
// quando existe, manda no badge — assim o AdsPower fica VERDE depois do teste,
// mesmo sem chave (o que vale é a conexão, não a chave).
function GroupStatus({ g, live }) {
  const pill = (cls, icon, text) => (
    <span className={`inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-full ${cls}`}>
      {icon}{text}
    </span>
  )
  const green = "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300"
  const red = "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300"
  const amber = "bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300"
  const blue = "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
  const gray = "bg-neutral-100 dark:bg-neutral-800 text-neutral-500 dark:text-neutral-400"

  // Resultado do teste ao vivo tem prioridade.
  if (live === "ok") return pill(green, <CheckCircle2 size={13} />, "conectada")
  if (live === "fail") return pill(red, <XCircle size={13} />, "com problema")
  if (live === "missing") return pill(red, <AlertTriangle size={13} />, "falta conectar")
  if (live === "warn") return pill(amber, <AlertTriangle size={13} />, "atenção")

  // Estado estático (antes de clicar em Verificar).
  if (g.connected) return pill(green, <CheckCircle2 size={13} />, g.required ? "conectada" : "conectada (opcional)")
  if (g.required) return pill(red, <AlertTriangle size={13} />, "falta conectar")
  if (g.id === "browser") return pill(blue, <Chrome size={13} />, "app essencial · chave opcional")
  return pill(gray, null, "opcional")
}

// Aviso da extensão do CapSolver — a chave sozinha não resolve o captcha no
// navegador; cada perfil do AdsPower precisa da extensão carregada.
function CapSolverExtensionNote() {
  return (
    <div className="mt-4 rounded-lg border-2 border-amber-300 dark:border-amber-800 bg-amber-50/70 dark:bg-amber-950/20 p-3.5">
      <div className="flex items-center gap-2 mb-2">
        <Puzzle size={17} className="text-amber-600 shrink-0" />
        <h4 className="text-sm font-bold text-amber-900 dark:text-amber-100">Falta 1 passo: instale a extensão do CapSolver no AdsPower</h4>
      </div>
      <p className="text-xs text-amber-900/90 dark:text-amber-100/90 leading-snug mb-3">
        A chave acima <b>sozinha não resolve</b> o reCAPTCHA. Quem resolve dentro do
        navegador é a <b>extensão do CapSolver</b> — ela precisa estar carregada em
        <b> cada perfil</b> do AdsPower.
      </p>
      <div className="flex flex-wrap gap-2 mb-3">
        <a href="https://github.com/capsolver/capsolver-browser-extension/releases" target="_blank" rel="noreferrer"
           className="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-2 rounded-lg bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 hover:opacity-90">
          <Download size={14} /> Baixar extensão (GitHub)
        </a>
        <a href="https://github.com/capsolver/capsolver-browser-extension" target="_blank" rel="noreferrer"
           className="inline-flex items-center gap-1.5 text-xs font-medium px-3 py-2 rounded-lg border border-amber-400 dark:border-amber-700 text-amber-800 dark:text-amber-200 hover:bg-amber-100 dark:hover:bg-amber-900/30">
          <ExternalLink size={13} /> Repositório
        </a>
        <a href="https://docs.capsolver.com/guide/extension/instructions.html" target="_blank" rel="noreferrer"
           className="inline-flex items-center gap-1.5 text-xs font-medium px-3 py-2 rounded-lg border border-amber-400 dark:border-amber-700 text-amber-800 dark:text-amber-200 hover:bg-amber-100 dark:hover:bg-amber-900/30">
          <ExternalLink size={13} /> Documentação
        </a>
      </div>
      <ol className="text-xs text-amber-900/90 dark:text-amber-100/90 leading-relaxed list-decimal pl-5 space-y-1.5">
        <li><b>Baixe e descompacte</b> a extensão (a pasta precisa ter o <span className="font-mono">manifest.json</span>). Você já tem uma cópia em <span className="font-mono">D:\capsolver-browser-extension-main</span>.</li>
        <li>Abra <span className="font-mono">assets\config.js</span> e cole a <b>sua</b> chave em <span className="font-mono">apiKey: 'CAP-…'</span> (a mesma da caixa acima). Confira que <span className="font-mono">useCapsolver: true</span>.</li>
        <li>No <b>AdsPower</b>: menu <b>Extensões</b> → aba <b>Extensões locais</b> → <b>Carregar extensão</b> → aponte para essa pasta.</li>
        <li>Ao <b>criar/editar cada perfil</b>, marque a extensão CapSolver em <b>“Aplicação de extensão”</b> — assim ela abre em todo navegador.</li>
        <li>Inicie o perfil: a extensão resolve o reCAPTCHA sozinha. ✅</li>
      </ol>
    </div>
  )
}

// Nota da API local do AdsPower (URL + quando a API Key é exigida).
function AdsPowerApiNote() {
  return (
    <div className="mt-4 rounded-lg border border-blue-200 dark:border-blue-900 bg-blue-50/60 dark:bg-blue-950/20 p-3.5 text-xs text-blue-900/90 dark:text-blue-100/90 leading-snug">
      <div className="flex items-center gap-2 mb-1.5">
        <Info size={15} className="text-blue-600 shrink-0" />
        <b>Sobre a API local do AdsPower</b>
      </div>
      <ul className="list-disc pl-5 space-y-1">
        <li>A conexão fica em <span className="font-mono">http://127.0.0.1:50325</span> (a porta pode mudar — confira em <b>AdsPower → Configurações de API</b>).</li>
        <li>A <b>API Key</b> só é exigida se você <b>ligar</b> a opção <b>“Verificação de API”</b> no AdsPower. Desligada (padrão), pode deixar o campo acima em branco.</li>
        <li>Se precisar, a chave está em <b>AdsPower → Configurações de API → API Key</b> (o botão <b>“Repor”</b> gera outra).</li>
      </ul>
    </div>
  )
}

// Visual de cada status do verificador ao vivo.
const STATUS_UI = {
  ok:      { Icon: CheckCircle2,  cls: "text-green-600 dark:text-green-400", chip: "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300", word: "OK" },
  fail:    { Icon: XCircle,       cls: "text-red-600 dark:text-red-400",     chip: "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300",         word: "PROBLEMA" },
  missing: { Icon: AlertTriangle, cls: "text-red-600 dark:text-red-400",     chip: "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300",         word: "FALTANDO" },
  warn:    { Icon: AlertTriangle, cls: "text-amber-600 dark:text-amber-400", chip: "bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300", word: "ATENÇÃO" },
}

export default function Apis() {
  const [groups, setGroups] = useState([])
  const [checker, setChecker] = useState(null)
  const [form, setForm] = useState({})      // { ENV_KEY: 'novo valor' }
  const [loading, setLoading] = useState(true)
  const [phase, setPhase] = useState('idle') // idle | saving | restarting | clearing
  const [msg, setMsg] = useState(null)       // { ok, text }
  const [confirmClear, setConfirmClear] = useState(false)
  const [checkResult, setCheckResult] = useState(null) // resposta do /apis/check
  const [checking, setChecking] = useState(false)

  const load = () => {
    setLoading(true)
    api.getApis()
      .then((r) => { setGroups(r.groups || []); setChecker(r.checker || null); setForm({}) })
      .catch((e) => setMsg({ ok: false, text: e.message }))
      .finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  // Espera o backend voltar depois do restart (fetch falha enquanto está caindo).
  const waitForBackend = async () => {
    const deadline = Date.now() + 60000
    await new Promise((r) => setTimeout(r, 2500))
    while (Date.now() < deadline) {
      try { await api.health(); return true }
      catch { await new Promise((r) => setTimeout(r, 1500)) }
    }
    return false
  }

  const filled = Object.entries(form).filter(([, v]) => v && v.trim())

  const saveAndRestart = async () => {
    setMsg(null)
    if (filled.length === 0) {
      setMsg({ ok: false, text: 'Nada para salvar — preencha ao menos uma API (campos em branco mantêm a atual).' })
      return
    }
    setPhase('saving')
    try {
      const values = Object.fromEntries(filled.map(([k, v]) => [k, v.trim()]))
      const res = await api.saveApis(values)
      setGroups(res.groups || [])
      setChecker(res.checker || null)
      setForm({})
      // Salvou → reinicia pra recarregar o .env.
      setPhase('restarting')
      setMsg({ ok: true, text: `✓ ${res.updated.length} API(s) salva(s) com sucesso! Reiniciando o bot para aplicar…` })
      try { await api.restartBackend() } catch { /* conexão cai antes de responder */ }
      const back = await waitForBackend()
      if (back) {
        setMsg({ ok: true, text: '✅ Alterações salvas com sucesso e bot reiniciado! Atualizando o painel…' })
        setTimeout(() => window.location.reload(), 1200)
      } else {
        setMsg({ ok: false, text: 'As APIs foram salvas, mas o bot não voltou em 60s. Confira a janela do backend (INICIAR_BACKEND.bat).' })
        setPhase('idle')
      }
    } catch (e) {
      setMsg({ ok: false, text: e.message })
      setPhase('idle')
    }
  }

  const clearAll = async () => {
    setMsg(null)
    setConfirmClear(false)
    setPhase('clearing')
    try {
      const res = await api.clearApis()
      setGroups(res.groups || [])
      setChecker(res.checker || null)
      setForm({})
      setPhase('restarting')
      setMsg({ ok: true, text: `🧹 ${res.cleared.length} chave(s) apagada(s). Reiniciando o bot para aplicar…` })
      try { await api.restartBackend() } catch { /* conexão cai antes de responder */ }
      const back = await waitForBackend()
      if (back) {
        setMsg({ ok: true, text: '✅ Todas as APIs foram limpas e o bot reiniciou! Atualizando o painel…' })
        setTimeout(() => window.location.reload(), 1200)
      } else {
        setMsg({ ok: false, text: 'As APIs foram limpas, mas o bot não voltou em 60s. Confira a janela do backend (INICIAR_BACKEND.bat).' })
        setPhase('idle')
      }
    } catch (e) {
      setMsg({ ok: false, text: e.message })
      setPhase('idle')
    }
  }

  const runCheck = async () => {
    setChecking(true)
    setMsg(null)
    try {
      const r = await api.checkApis()
      setCheckResult(r)
    } catch (e) {
      setMsg({ ok: false, text: 'Não deu pra verificar as APIs: ' + e.message })
    } finally {
      setChecking(false)
    }
  }

  const busy = phase !== 'idle'
  // Status do último "Verificar APIs" por grupo — manda nos badges dos cards.
  const liveById = Object.fromEntries((checkResult?.results || []).map((r) => [r.id, r.status]))

  return (
    <>
      <PageHead title="APIs" subtitle="Troque as chaves das ferramentas do bot — salve tudo e reinicie" />

      {/* Instruções */}
      <Card className="max-w-3xl mb-6 border-2 border-blue-200 dark:border-blue-900 bg-gradient-to-br from-blue-50 to-cyan-50 dark:from-blue-950/30 dark:to-cyan-950/30">
        <div className="flex items-center gap-2 mb-3">
          <Info size={20} className="text-blue-600" />
          <h3 className="text-lg font-bold text-blue-900 dark:text-blue-100">Como usar</h3>
        </div>
        <ul className="text-sm text-blue-900 dark:text-blue-100 space-y-1.5 leading-relaxed list-disc pl-5">
          <li>Quer usar <b>as suas próprias ferramentas</b> (para gastar o seu saldo)? Cole as suas chaves aqui.</li>
          <li>Em cada API tem o <b>site oficial</b> (link) e <b>o que copiar</b> lá dentro — é só seguir.</li>
          <li>Campo <b>em branco = mantém</b> a chave que já está salva. Preencha só o que quiser trocar.</li>
          <li>Chaves já configuradas aparecem <b>mascaradas</b> (ex.: <span className="font-mono">nv_l…c51</span>) — por segurança, nunca mostramos o valor inteiro.</li>
          <li>Ao clicar em <b>Salvar tudo e reiniciar</b>, gravamos no <span className="font-mono">.env</span> e reiniciamos o bot para aplicar. Runs, BMs e estoque ficam salvos.</li>
          <li><b>Domínios Netlify/Cloudflare</b> ficam na aba <b>Credenciais</b> (têm o passo dos nameservers).</li>
        </ul>
      </Card>

      {/* Checker — o fluxo só roda com todas as obrigatórias conectadas */}
      {checker && (
        <Card className={`max-w-3xl mb-6 border-2 ${
          checker.ready
            ? "border-green-300 dark:border-green-900 bg-gradient-to-br from-green-50 to-emerald-50 dark:from-green-950/30 dark:to-emerald-950/20"
            : "border-amber-300 dark:border-amber-900 bg-gradient-to-br from-amber-50 to-orange-50 dark:from-amber-950/30 dark:to-orange-950/20"
        }`}>
          <div className="flex items-center gap-2 mb-2">
            {checker.ready
              ? <CheckCircle2 size={20} className="text-green-600" />
              : <AlertTriangle size={20} className="text-amber-600" />}
            <h3 className={`text-lg font-bold ${checker.ready ? "text-green-900 dark:text-green-100" : "text-amber-900 dark:text-amber-100"}`}>
              {checker.ready ? "Tudo pronto — o fluxo pode rodar" : "Faltam APIs para o fluxo rodar"}
            </h3>
          </div>
          <p className="text-sm text-neutral-700 dark:text-neutral-200 mb-3">
            <b>{checker.required_connected}/{checker.required_total}</b> APIs obrigatórias conectadas.
            {checker.ready
              ? " Nenhuma pendência bloqueia as runs."
              : " Conecte as que estão faltando abaixo."}
          </p>
          {!checker.ready && checker.missing.length > 0 && (
            <div className="mb-2">
              <span className="text-xs font-semibold text-red-700 dark:text-red-300 flex items-center gap-1 mb-1">
                <AlertTriangle size={13} /> Faltando (obrigatórias):
              </span>
              <div className="flex flex-wrap gap-1.5">
                {checker.missing.map((m) => (
                  <span key={m} className="text-xs font-medium px-2.5 py-1 rounded-full bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300">{m}</span>
                ))}
              </div>
            </div>
          )}
          {checker.optional_connected.length > 0 && (
            <div>
              <span className="text-xs font-semibold text-blue-700 dark:text-blue-300 flex items-center gap-1 mb-1">
                <CheckCircle2 size={13} /> Extras conectadas (opcionais):
              </span>
              <div className="flex flex-wrap gap-1.5">
                {checker.optional_connected.map((m) => (
                  <span key={m} className="text-xs font-medium px-2.5 py-1 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300">{m}</span>
                ))}
              </div>
            </div>
          )}

          {/* Botão de verificação AO VIVO (testa saldo/conexão de verdade) */}
          <div className="mt-4 pt-3 border-t border-neutral-200/70 dark:border-neutral-700/50 flex items-center gap-3 flex-wrap">
            <button
              onClick={runCheck}
              disabled={checking || busy}
              className={`${btnPrimary} px-5 py-2.5 font-semibold flex items-center gap-2 disabled:opacity-50`}
            >
              {checking
                ? <><RefreshCw size={16} className="animate-spin" /> Verificando ao vivo…</>
                : <><Activity size={16} /> Verificar APIs agora</>}
            </button>
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              Testa de verdade: saldo do SMS e do captcha, e se o AdsPower está aberto.
            </span>
          </div>
        </Card>
      )}

      {/* Resultado da verificação ao vivo */}
      {checkResult && (
        <Card className={`max-w-3xl mb-6 border-2 ${
          checkResult.ok
            ? "border-green-300 dark:border-green-900 bg-gradient-to-br from-green-50 to-emerald-50 dark:from-green-950/30 dark:to-emerald-950/20"
            : "border-red-300 dark:border-red-900 bg-gradient-to-br from-red-50 to-orange-50 dark:from-red-950/30 dark:to-orange-950/20"
        }`}>
          <div className="flex items-center gap-2 mb-1">
            {checkResult.ok
              ? <CheckCircle2 size={20} className="text-green-600" />
              : <XCircle size={20} className="text-red-600" />}
            <h3 className={`text-lg font-bold ${checkResult.ok ? "text-green-900 dark:text-green-100" : "text-red-900 dark:text-red-100"}`}>
              {checkResult.ok ? "Tudo certo — pode rodar!" : "Encontramos problemas nas APIs"}
            </h3>
          </div>
          <p className="text-sm text-neutral-700 dark:text-neutral-200 mb-3">
            <b>{checkResult.summary.required_ok}/{checkResult.summary.required_total}</b> obrigatórias OK.
            {checkResult.ok
              ? " Nenhum bloqueio para as runs."
              : ` ${checkResult.summary.problems.length} problema(s) a resolver antes de rodar.`}
          </p>

          {/* Lista item a item */}
          <div className="grid gap-2 mb-1">
            {checkResult.results.map((r) => {
              const ui = STATUS_UI[r.status] || STATUS_UI.warn
              const Icon = ui.Icon
              return (
                <div key={r.id} className="flex items-start gap-2.5 rounded-lg bg-white/60 dark:bg-neutral-900/50 border border-neutral-200/70 dark:border-neutral-800 px-3 py-2">
                  <Icon size={17} className={`${ui.cls} mt-0.5 shrink-0`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-semibold text-neutral-800 dark:text-neutral-100">{r.label}</span>
                      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${ui.chip}`}>{ui.word}</span>
                      {r.required && <span className="text-[10px] font-medium text-red-600 dark:text-red-400">obrigatória</span>}
                      {r.live && <span className="text-[10px] font-medium text-neutral-400 flex items-center gap-0.5"><Activity size={10} /> ao vivo</span>}
                    </div>
                    <p className="text-xs text-neutral-600 dark:text-neutral-300 leading-snug mt-0.5">{r.detail}</p>
                  </div>
                </div>
              )
            })}
          </div>

          {checkResult.summary.problems.length > 0 && (
            <div className="mt-3 p-3 rounded-lg bg-red-100/70 dark:bg-red-900/30">
              <span className="text-xs font-bold text-red-800 dark:text-red-200 flex items-center gap-1 mb-1">
                <XCircle size={13} /> Resolva antes de rodar:
              </span>
              <ul className="text-xs text-red-700 dark:text-red-300 list-disc pl-5 space-y-0.5">
                {checkResult.summary.problems.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            </div>
          )}
        </Card>
      )}

      {loading ? (
        <Card className="max-w-3xl"><p className="text-sm text-neutral-500">Carregando APIs…</p></Card>
      ) : (
        <div className="max-w-3xl grid gap-5">
          {groups.map((g) => {
            const Icon = GROUP_ICON[g.id] || KeyRound
            const live = liveById[g.id]                       // status do teste ao vivo
            const good = live === "ok" || (!live && g.connected)
            const bad = live === "fail" || live === "missing" || (!live && g.required && !g.connected)
            return (
              <Card key={g.id} className={`border-2 ${
                good
                  ? "border-green-200 dark:border-green-900/60"
                  : bad
                    ? "border-red-200 dark:border-red-900/60"
                    : "border-neutral-200 dark:border-neutral-800"
              }`}>
                <div className="flex items-center justify-between gap-2 mb-1.5 flex-wrap">
                  <div className="flex items-center gap-2">
                    <Icon size={19} className="text-lime-600 dark:text-lime-400" />
                    <h3 className="text-base font-bold text-neutral-900 dark:text-neutral-100">{g.label}</h3>
                    {g.required && <span className="text-[10px] font-bold uppercase tracking-wide text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40 px-1.5 py-0.5 rounded">obrigatória</span>}
                  </div>
                  <GroupStatus g={g} live={live} />
                </div>
                <p className="text-xs text-neutral-500 dark:text-neutral-400 mb-4">{g.desc}</p>
                <div className="grid gap-4">
                  {g.fields.map((f) => (
                    <label key={f.key} className="text-sm block rounded-lg border border-neutral-200 dark:border-neutral-800 p-3 bg-neutral-50/50 dark:bg-neutral-900/40">
                      <span className="flex items-center justify-between mb-1.5 gap-2 flex-wrap">
                        <span className="font-semibold text-neutral-800 dark:text-neutral-200">
                          {f.label}
                          {f.alt && <span className="text-[10px] font-medium text-neutral-400 ml-1.5">(alternativa)</span>}
                          {!f.alt && f.required === false && <span className="text-[10px] font-medium text-neutral-400 ml-1.5">(opcional)</span>}
                        </span>
                        {f.set
                          ? <Badge tone="green">✓ {f.secret === false ? 'definida' : f.hint}</Badge>
                          : f.required
                            ? <span className="text-xs bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 px-2.5 py-1 rounded-full font-medium">não configurada</span>
                            : <span className="text-xs bg-neutral-100 dark:bg-neutral-800 text-neutral-500 dark:text-neutral-400 px-2.5 py-1 rounded-full font-medium">opcional — não precisa</span>}
                      </span>

                      {/* Onde pegar: site + o que copiar */}
                      <div className="flex flex-col gap-1 mb-2 text-xs">
                        {f.url && (
                          <a href={f.url} target="_blank" rel="noreferrer"
                             className="inline-flex items-center gap-1 w-fit font-medium text-blue-600 dark:text-blue-400 hover:underline">
                            <ExternalLink size={12} /> {f.site || f.url}
                          </a>
                        )}
                        {f.get && (
                          <span className="text-neutral-600 dark:text-neutral-300">
                            <b className="text-neutral-500 dark:text-neutral-400">Onde pegar:</b> {f.get}
                          </span>
                        )}
                      </div>

                      <input
                        type={f.secret === false ? 'text' : 'password'}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={busy}
                        placeholder={f.secret === false
                          ? (f.value || 'http://127.0.0.1:50325')
                          : (f.set ? '••••• salva — deixe vazio p/ manter' : 'cole a chave aqui')}
                        value={form[f.key] !== undefined ? form[f.key] : (f.secret === false ? (f.value || '') : '')}
                        onChange={(e) => set(f.key, e.target.value)}
                        className={inputCls}
                      />
                      {f.help && <span className="block text-xs text-neutral-500 dark:text-neutral-400 mt-1.5 leading-snug">{f.help}</span>}
                    </label>
                  ))}
                </div>
                {g.id === "captcha" && <CapSolverExtensionNote />}
                {g.id === "browser" && <AdsPowerApiNote />}
              </Card>
            )
          })}

          {/* Barra de ação — salvar tudo + reiniciar */}
          <Card className="border-2 border-lime-300 dark:border-lime-900 bg-gradient-to-br from-lime-50 to-emerald-50 dark:from-lime-950/30 dark:to-emerald-950/30 sticky bottom-4 shadow-lg">
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <div className="text-sm text-neutral-700 dark:text-neutral-200">
                {filled.length > 0
                  ? <span><b>{filled.length}</b> API(s) preenchida(s) para trocar.</span>
                  : <span className="text-neutral-500">Preencha as APIs que quiser trocar.</span>}
              </div>
              <button
                onClick={saveAndRestart}
                disabled={busy || filled.length === 0}
                className={`${btnPrimary} px-6 py-3 font-semibold flex items-center gap-2 disabled:opacity-50`}
              >
                {phase === 'saving' && <><Save size={17} /> Salvando…</>}
                {phase === 'restarting' && <><RefreshCw size={17} className="animate-spin" /> Reiniciando o bot…</>}
                {phase === 'clearing' && <><RefreshCw size={17} className="animate-spin" /> Limpando…</>}
                {phase === 'idle' && <><Save size={17} /> 💾 Salvar tudo e reiniciar</>}
              </button>
            </div>
            {msg && (
              <p className={`text-sm mt-4 p-3 rounded-lg font-medium ${msg.ok ? 'text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/40' : 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40'}`}>
                {msg.text}
              </p>
            )}
          </Card>

          {/* Zona de perigo — limpar todas as APIs (p/ entregar o bot sem vazar saldo) */}
          <Card className="border-2 border-red-200 dark:border-red-900/60 bg-red-50/40 dark:bg-red-950/20">
            <div className="flex items-start gap-2 mb-3">
              <Trash2 size={18} className="text-red-600 mt-0.5 shrink-0" />
              <div>
                <h3 className="text-base font-bold text-red-800 dark:text-red-200">Limpar todas as APIs</h3>
                <p className="text-xs text-red-700/80 dark:text-red-300/80 mt-1 leading-snug max-w-xl">
                  Apaga <b>todas</b> as chaves de serviço (SMS, captcha, CNPJ, DataCrazy, AdsPower).
                  Use antes de entregar o bot pra outra pessoa — assim ela não gasta o seu saldo/créditos.
                  <br />Os <b>domínios e hospedagem</b> (Netlify/Cloudflare, aba Credenciais) <b>não</b> são apagados.
                </p>
              </div>
            </div>
            {!confirmClear ? (
              <button
                onClick={() => { setMsg(null); setConfirmClear(true) }}
                disabled={busy}
                className="px-4 py-2.5 rounded-lg text-sm font-semibold flex items-center gap-2 bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-900/60 disabled:opacity-50 transition"
              >
                <Trash2 size={16} /> Limpar todas as APIs
              </button>
            ) : (
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-semibold text-red-800 dark:text-red-200">Tem certeza? Isso apaga todas as suas chaves.</span>
                <button
                  onClick={clearAll}
                  disabled={busy}
                  className="px-4 py-2.5 rounded-lg text-sm font-bold flex items-center gap-2 bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 transition"
                >
                  <Trash2 size={16} /> Sim, limpar tudo e reiniciar
                </button>
                <button
                  onClick={() => setConfirmClear(false)}
                  disabled={busy}
                  className="px-4 py-2.5 rounded-lg text-sm font-semibold text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800 disabled:opacity-50 transition"
                >
                  Cancelar
                </button>
              </div>
            )}
          </Card>
        </div>
      )}
    </>
  )
}

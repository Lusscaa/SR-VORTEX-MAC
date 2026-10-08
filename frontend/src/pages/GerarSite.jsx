import { useEffect, useMemo, useRef, useState } from "react"
import {
  Globe, Search, Save, RefreshCw, ExternalLink, CheckCircle,
  AlertCircle, Building2, Tag, Eye, Pencil, Trash2, Rocket, Hand,
  LayoutGrid, PlusCircle, Copy, ChevronLeft, ChevronRight, FileText, Download,
  Database, ChevronDown, ChevronUp,
} from "lucide-react"
import { api } from "../api.js"
import { Card, PageHead, btnPrimary, inputCls, Badge, CopyButton } from "../components/ui.jsx"

// CNPJ 14 dígitos → 00.000.000/0000-00.
function fmtCnpj(v) {
  const d = String(v || "").replace(/\D/g, "")
  if (d.length !== 14) return v || ""
  return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`
}

// Telefone BR no formato do CNPJ: (DD) 98212-0535 (11 díg.) ou (DD) 9821-2053 (10).
function fmtPhone(v) {
  const d = String(v || "").replace(/\D/g, "").slice(0, 11)
  if (!d) return ""
  if (d.length <= 2) return `(${d}`
  if (d.length <= 6) return `(${d.slice(0, 2)}) ${d.slice(2)}`
  if (d.length <= 10) return `(${d.slice(0, 2)}) ${d.slice(2, 6)}-${d.slice(6)}`
  return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7, 11)}`
}

// Linha de dado com rótulo + valor + botão de copiar (some se não tiver valor).
function InfoRow({ label, value, copy }) {
  const has = value && value !== "—"
  return (
    <div className="flex items-center gap-1.5 min-w-0">
      <span className="text-[11px] uppercase tracking-wide text-neutral-400 dark:text-emerald-300/50 shrink-0">{label}:</span>
      <span className="text-xs font-semibold truncate text-neutral-800 dark:text-emerald-50" title={value || "—"}>{has ? value : "—"}</span>
      {has && <CopyButton text={copy ?? value} label={<Copy size={12} />} className="shrink-0 text-neutral-400 hover:text-emerald-500 dark:hover:text-emerald-300 text-[10px]" />}
    </div>
  )
}

const FIELD_META = [
  { key: "razao_social", label: "Razão social", full: true },
  { key: "nome_fantasia", label: "Nome fantasia", full: true },
  { key: "situacao", label: "Situação cadastral" },
  { key: "abertura", label: "Data de abertura" },
  { key: "natureza_juridica", label: "Natureza jurídica", full: true },
  { key: "porte", label: "Porte" },
  { key: "capital_social", label: "Capital social" },
  { key: "cnae_principal", label: "Atividade principal (CNAE)", full: true },
  { key: "logradouro", label: "Logradouro", full: true },
  { key: "numero", label: "Número" },
  { key: "complemento", label: "Complemento" },
  { key: "bairro", label: "Bairro" },
  { key: "cidade", label: "Cidade" },
  { key: "uf", label: "UF" },
  { key: "cep", label: "CEP" },
  { key: "telefone", label: "Telefone" },
  { key: "email", label: "Email" },
]
const EMPTY_FIELDS = Object.fromEntries(FIELD_META.map((f) => [f.key, ""]))
const NI = "Não informado"

function buildEmpresa(fields, digits) {
  const v = (k, d = "") => (String(fields[k] || "").trim()) || d
  const cnpj = digits.length === 14
    ? `${digits.slice(0, 2)}.${digits.slice(2, 5)}.${digits.slice(5, 8)}/${digits.slice(8, 12)}-${digits.slice(12)}`
    : (digits || "")
  return {
    cnpj, razaoSocial: v("razao_social", NI), nomeFantasia: v("nome_fantasia", NI),
    abertura: v("abertura", NI), situacao: v("situacao", "Ativa"),
    naturezaJuridica: v("natureza_juridica", NI), porte: v("porte", NI), mei: NI,
    capitalSocial: v("capital_social", NI), cnaePrincipal: v("cnae_principal", NI),
    atividadesSecundarias: [],
    endereco: {
      logradouro: v("logradouro", NI), numero: v("numero", "S/N"),
      complemento: v("complemento"), bairro: v("bairro", NI),
      cidade: v("cidade", NI), uf: v("uf", NI), cep: v("cep", NI),
    },
    contato: { telefone: v("telefone", NI), email: v("email", NI) },
  }
}
const b64utf8 = (s) => btoa(unescape(encodeURIComponent(s)))

export default function GerarSite() {
  const [tab, setTab] = useState("gerar")   // 'gerar' | 'meus'
  const [cnpj, setCnpj] = useState("")
  const [fields, setFields] = useState(EMPTY_FIELDS)
  const [slug, setSlug] = useState("")
  const [domain, setDomain] = useState("")
  const [domains, setDomains] = useState([])
  const [metatag, setMetatag] = useState("")
  const [existing, setExisting] = useState(null)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)
  const [result, setResult] = useState(null)
  const [previewSrc, setPreviewSrc] = useState("")
  const [template, setTemplate] = useState("professional")
  const [poolCnpjs, setPoolCnpjs] = useState([])
  const [poolMeta, setPoolMeta] = useState({})
  const [poolOpen, setPoolOpen] = useState(false)

  const setField = (k, v) => setFields((f) => ({ ...f, [k]: v }))

  // Pool de CNPJs — carrega uma vez e consulta dados sequencialmente.
  const loadPool = () => {
    api.pools().then((r) => setPoolCnpjs((r.cnpjs || []).filter((x) => x.status === "available"))).catch(() => {})
  }
  useEffect(() => { loadPool() }, [])

  const poolKey = poolCnpjs.map((x) => x.cnpj).join(",")
  useEffect(() => {
    let cancelled = false
    const list = poolKey ? poolKey.split(",") : []
    ;(async () => {
      for (const cn of list) {
        if (cancelled) return
        let skip = false
        setPoolMeta((m) => { if (m[cn]) skip = true; return m })
        if (skip) continue
        setPoolMeta((m) => ({ ...m, [cn]: { loading: true } }))
        try {
          const d = await api.cnpjInfo(cn)
          if (cancelled) return
          setPoolMeta((m) => ({ ...m, [cn]: { data: d } }))
        } catch (e) {
          if (cancelled) return
          setPoolMeta((m) => ({ ...m, [cn]: { err: e.message } }))
        }
      }
    })()
    return () => { cancelled = true }
  }, [poolKey])

  const previewUrl = useMemo(() => {
    if (!loaded || !domain) return ""
    try {
      const emp = buildEmpresa(fields, (cnpj || "").replace(/\D+/g, ""))
      const base = `https://preview.${domain}/?__preview=${encodeURIComponent(b64utf8(JSON.stringify(emp)))}`
      return template === "professional" ? `${base}&__template=professional` : base
    } catch { return "" }
  }, [fields, domain, loaded, cnpj, template])

  useEffect(() => {
    if (!previewUrl) return
    const t = setTimeout(() => setPreviewSrc(previewUrl), 700)
    return () => clearTimeout(t)
  }, [previewUrl])

  const buscar = async (override) => {
    setMsg(null); setResult(null)
    const digits = String(override ?? cnpj ?? "").replace(/\D+/g, "")
    if (digits.length !== 14) { setMsg({ ok: false, text: "CNPJ precisa ter 14 dígitos." }); return }
    setCnpj(digits)
    setBusy("search")
    try {
      const r = await api.manualSitePreview(digits)
      setFields({ ...EMPTY_FIELDS, ...(r.fields || {}) })
      setSlug(r.slug || "")
      setDomains(r.domains || [])
      setDomain((r.already && r.already.domain) || r.base_domain || (r.domains?.[0]?.domain) || "")
      setExisting(r.already || null)
      setLoaded(true)
      setMsg({ ok: true, text: r.already
        ? `Este CNPJ já tem site em ${r.already.fqdn} — dados carregados para edição.`
        : "Dados carregados. Edite o que quiser e veja o preview ao lado." })
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setBusy(null) }
  }

  const publicar = async () => {
    setMsg(null)
    const digits = (cnpj || "").replace(/\D+/g, "")
    if (digits.length !== 14) { setMsg({ ok: false, text: "Busque um CNPJ primeiro." }); return }
    if (!domain) { setMsg({ ok: false, text: "Escolha um domínio." }); return }
    setBusy("publish")
    try {
      const r = await api.manualSitePublish({ cnpj: digits, slug: slug || null, metatag: metatag || null, domain, fields, template })
      setResult(r)
      setExisting({ fqdn: r.fqdn, domain: r.domain, token: r.token, url: r.url, source: "manual" })
      setMsg({ ok: true, text: r.message || "Publicado!" })
      loadPool()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    finally { setBusy(null) }
  }

  // Editar (vindo de Meus Sites): carrega o CNPJ no form e volta pra aba Gerar.
  const editarSite = (site) => {
    setTab("gerar")
    buscar(site.cnpj)
  }

  // Limpa o formulário pra criar outro domínio sem precisar dar F5.
  const novoSite = () => {
    setCnpj(""); setFields(EMPTY_FIELDS); setSlug(""); setDomain(""); setDomains([])
    setMetatag(""); setTemplate("professional"); setExisting(null); setLoaded(false)
    setResult(null); setPreviewSrc(""); setMsg(null)
  }

  const jaExiste = !!existing
  const domSel = domains.find((d) => d.domain === domain)

  const TabBtn = ({ id, icon: Icon, children }) => (
    <button
      onClick={() => setTab(id)}
      className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold transition ${
        tab === id
          ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
          : "text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800"
      }`}
    >
      <Icon size={16} /> {children}
    </button>
  )

  return (
    <>
      <PageHead title="Gerar Site / PDFs" subtitle="Publique o site de um CNPJ, gerencie seus sites e os PDFs (comprovantes) das runs" />

      <div className="flex items-center gap-2 mb-5 flex-wrap">
        <TabBtn id="gerar" icon={PlusCircle}>Gerar Site</TabBtn>
        <TabBtn id="meus" icon={LayoutGrid}>Meus Sites</TabBtn>
        <TabBtn id="pdfs" icon={FileText}>PDFs / CNPJs</TabBtn>
      </div>

      {tab === "meus" ? (
        <MeusSites onEdit={editarSite} />
      ) : tab === "pdfs" ? (
        <PdfsCnpjs />
      ) : (
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(0,560px)] gap-5">
        {/* ===== FORM ===== */}
        <div>
          <Card className="mb-5 border-2 border-lime-300 dark:border-lime-900 bg-gradient-to-br from-lime-50 to-emerald-50 dark:from-lime-950/20 dark:to-emerald-950/20">
            <div className="flex items-center gap-2 mb-3">
              <span className="grid h-6 w-6 place-items-center rounded-full bg-lime-500 text-neutral-900 font-bold text-xs">1</span>
              <Search size={18} className="text-lime-600" />
              <h3 className="text-base font-bold">Consultar CNPJ (preenchimento automático)</h3>
            </div>
            <div className="flex gap-2">
              <input className={inputCls} placeholder="00.000.000/0000-00" value={cnpj}
                onChange={(e) => setCnpj(e.target.value)} onKeyDown={(e) => e.key === "Enter" && buscar()} />
              <button onClick={() => buscar()} disabled={busy === "search"} className={`${btnPrimary} px-6 whitespace-nowrap flex items-center gap-2`}>
                {busy === "search" ? <RefreshCw size={16} className="animate-spin" /> : <Search size={16} />}
                {busy === "search" ? "Buscando…" : "Buscar"}
              </button>
            </div>
            <p className="text-xs text-neutral-500 mt-2">Preenche razão social, endereço, telefone e atividade. Você edita o que quiser depois.</p>
            {poolCnpjs.length > 0 && (
              <div className="mt-3 border-t border-lime-200 dark:border-lime-900/60 pt-3">
                <button type="button" onClick={() => setPoolOpen((v) => !v)}
                  className="flex items-center gap-2 text-xs font-semibold text-lime-700 dark:text-lime-400 hover:underline">
                  <Database size={14} />
                  CNPJs no pool ({poolCnpjs.length})
                  {poolOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </button>
                {poolOpen && (
                  <div className="mt-2 max-h-52 overflow-y-auto space-y-1 pr-1">
                    {poolCnpjs.map((it) => {
                      const meta = poolMeta[it.cnpj]
                      return (
                        <button key={it.cnpj} type="button"
                          onClick={() => { setCnpj(it.cnpj); buscar(it.cnpj) }}
                          className="w-full text-left px-3 py-2 rounded-lg border border-neutral-200 dark:border-neutral-700 hover:border-lime-400 hover:bg-lime-50 dark:hover:bg-lime-950/30 transition group">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-mono text-sm">{fmtCnpj(it.cnpj)}</span>
                            <span className="text-[10px] font-semibold text-lime-600 dark:text-lime-400 opacity-0 group-hover:opacity-100 transition">USAR →</span>
                          </div>
                          {meta?.loading && <div className="text-[11px] text-neutral-400 mt-0.5">consultando…</div>}
                          {meta?.data && (
                            <div className="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5 truncate">
                              {meta.data.razao_social || "sem razão social"}
                              {meta.data.telefone ? ` · tel. ${fmtPhone(meta.data.telefone)}` : ""}
                            </div>
                          )}
                          {meta?.err && <div className="text-[11px] text-red-500 mt-0.5 truncate">{meta.err}</div>}
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )}
          </Card>

          {loaded && (
            <>
              <Card className="mb-5 border-2 border-neutral-200 dark:border-neutral-800">
                <div className="flex items-center gap-2 mb-4">
                  <span className="grid h-6 w-6 place-items-center rounded-full bg-lime-500 text-neutral-900 font-bold text-xs">2</span>
                  <Building2 size={18} className="text-lime-600" />
                  <h3 className="text-base font-bold">Dados da empresa <span className="font-normal text-neutral-500 text-sm">(tudo editável)</span></h3>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {FIELD_META.map((f) => (
                    <label key={f.key} className={`text-sm ${f.full ? "sm:col-span-2" : ""}`}>
                      <span className="block text-xs font-semibold text-neutral-500 mb-1">{f.label}</span>
                      <input className={inputCls} value={fields[f.key] ?? ""} onChange={(e) => setField(f.key, e.target.value)} />
                    </label>
                  ))}
                </div>
              </Card>

              <Card className="mb-5 border-2 border-blue-200 dark:border-blue-900 bg-gradient-to-br from-blue-50 to-cyan-50 dark:from-blue-950/20 dark:to-cyan-950/20">
                <div className="flex items-center gap-2 mb-4">
                  <span className="grid h-6 w-6 place-items-center rounded-full bg-lime-500 text-neutral-900 font-bold text-xs">3</span>
                  <Globe size={18} className="text-blue-600" />
                  <h3 className="text-base font-bold">Publicar</h3>
                </div>
                <div className="mb-4">
                  <span className="block text-xs font-semibold text-neutral-500 mb-2">Template do site</span>
                  <div className="flex gap-3">
                    {[
                      { id: "professional", label: "Profissional", desc: "Claro, corporativo — ideal para aprovação Meta" },
                      { id: "classic", label: "Clássico", desc: "Escuro, original" },
                    ].map((t) => (
                      <button key={t.id} type="button" onClick={() => setTemplate(t.id)}
                        className={`flex-1 p-3 rounded-lg border-2 text-left transition ${
                          template === t.id
                            ? "border-blue-500 bg-blue-50 dark:bg-blue-950/30 dark:border-blue-400"
                            : "border-neutral-200 dark:border-neutral-700 hover:border-neutral-300 dark:hover:border-neutral-600"
                        }`}>
                        <span className={`block text-sm font-semibold ${template === t.id ? "text-blue-700 dark:text-blue-300" : "text-neutral-700 dark:text-neutral-300"}`}>{t.label}</span>
                        <span className="block text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">{t.desc}</span>
                      </button>
                    ))}
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                  <label className="text-sm">
                    <span className="block text-xs font-semibold text-neutral-500 mb-1">Subdomínio (sem espaços)</span>
                    <input className={inputCls} value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="nome-da-empresa" />
                  </label>
                  <label className="text-sm">
                    <span className="block text-xs font-semibold text-neutral-500 mb-1">Domínio</span>
                    <select className={inputCls} value={domain} onChange={(e) => setDomain(e.target.value)}>
                      {domains.length === 0 && <option value="">— nenhum domínio cadastrado —</option>}
                      {domains.map((d) => (
                        <option key={d.domain} value={d.domain}>{d.domain} ({d.remaining ?? "?"} livres de {d.cap ?? "?"})</option>
                      ))}
                    </select>
                  </label>
                </div>
                <div className="mb-2 text-sm">
                  <span className="text-neutral-500">O site ficará em </span>
                  <span className="font-mono font-bold text-blue-700 dark:text-blue-300">{(slug || "nome").toLowerCase()}.{domain || "dominio"}</span>
                  {domSel && Number(domSel.remaining) <= 0 && !jaExiste && <span className="text-red-600 ml-2">⚠️ sem cota livre</span>}
                </div>
                <label className="text-sm block mb-3">
                  <span className="text-xs font-semibold text-neutral-500 mb-1 flex items-center gap-1"><Tag size={13} /> Meta tag do Facebook — opcional</span>
                  <input className={inputCls} value={metatag} onChange={(e) => setMetatag(e.target.value)} placeholder='Cole a tag inteira ou só o content' />
                  <span className="text-xs text-neutral-500 mt-1 block">Pode publicar sem a metatag, adicionar depois e clicar em <b>Atualizar</b> — o site vincula na hora.</span>
                </label>
                <div className="flex items-center gap-3 flex-wrap">
                  <button onClick={publicar} disabled={busy === "publish" || !domain} className={`${btnPrimary} px-6 py-3 font-semibold flex items-center gap-2 disabled:opacity-50`}>
                    {busy === "publish"
                      ? <><RefreshCw size={17} className="animate-spin" /> {jaExiste ? "Atualizando…" : "Publicando…"}</>
                      : jaExiste ? <><RefreshCw size={17} /> Atualizar site</> : <><Save size={17} /> Publicar site</>}
                  </button>
                  {existing && (
                    <a href={existing.url} target="_blank" rel="noreferrer" className="text-sm text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1">
                      <ExternalLink size={14} /> abrir {existing.fqdn}
                    </a>
                  )}
                </div>
              </Card>
            </>
          )}

          {msg && (
            <div className={`mb-4 p-3 rounded-lg text-sm font-medium flex items-start gap-2 ${msg.ok ? "bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200" : "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300"}`}>
              {msg.ok ? <CheckCircle size={16} className="mt-0.5 shrink-0" /> : <AlertCircle size={16} className="mt-0.5 shrink-0" />}
              <span>{msg.text}</span>
            </div>
          )}
          {result && (
            <Card className="border-2 border-green-200 dark:border-green-900 bg-gradient-to-br from-green-50 to-emerald-50 dark:from-green-950/20 dark:to-emerald-950/20">
              <div className="flex flex-wrap items-center gap-2">
                <a href={result.url} target="_blank" rel="noreferrer" className="font-mono text-sm text-blue-700 dark:text-blue-300 hover:underline flex items-center gap-1">
                  <ExternalLink size={14} /> {result.url}
                </a>
                <Badge tone={result.probe?.site_ok ? "green" : "amber"}>{result.probe?.site_ok ? "no ar (200)" : `status ${result.probe?.status ?? "—"}`}</Badge>
                {result.token
                  ? <Badge tone={result.probe?.metatag_ok ? "green" : "amber"}>{result.probe?.metatag_ok ? "metatag no ar ✓" : "metatag salva (propagando)"}</Badge>
                  : <Badge tone="neutral">sem metatag ainda</Badge>}
              </div>
              <div className="mt-3 pt-3 border-t border-green-200 dark:border-green-900/60">
                <button onClick={novoSite} className={`${btnPrimary} px-5 py-2.5 font-semibold flex items-center gap-2`}>
                  <PlusCircle size={17} /> Criar novo domínio
                </button>
                <span className="text-xs text-neutral-500 mt-1.5 block">Limpa o formulário pra fazer outro site sem recarregar a página.</span>
              </div>
            </Card>
          )}
        </div>

        {/* ===== PREVIEW ===== */}
        <div className="xl:sticky xl:top-6 self-start">
          <Card className="border-2 border-neutral-200 dark:border-neutral-800 p-0 overflow-hidden">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-900/60">
              <div className="flex items-center gap-2">
                <Eye size={16} className="text-lime-600" />
                <span className="text-sm font-bold">Preview ao vivo</span>
                <span className="text-xs text-neutral-500">(não publicado)</span>
              </div>
              {previewSrc && (
                <a href={previewSrc} target="_blank" rel="noreferrer" className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1">
                  <ExternalLink size={12} /> abrir
                </a>
              )}
            </div>
            {loaded ? (
              <div className="bg-white">
                <iframe key={previewSrc} src={previewSrc || "about:blank"} title="preview" className="w-full border-0" style={{ height: "78vh" }} />
              </div>
            ) : (
              <div className="grid place-items-center text-center text-neutral-400 dark:text-neutral-500 px-6" style={{ height: "60vh" }}>
                <div>
                  <Eye size={40} className="mx-auto mb-3 opacity-50" />
                  <p className="text-sm">Busque um CNPJ para ver o preview do site aqui,<br />atualizando ao vivo conforme você edita.</p>
                </div>
              </div>
            )}
          </Card>
          <p className="text-xs text-neutral-500 mt-2 text-center">O preview usa o template real e atualiza ~1s após você parar de digitar. Nada é publicado.</p>
        </div>
      </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Meus Sites — todos os sites (manuais + de RUN), com editar/excluir
// ---------------------------------------------------------------------------
function MeusSites({ onEdit }) {
  const [sites, setSites] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState("all")   // all | manual | run
  const [q, setQ] = useState("")
  const [msg, setMsg] = useState(null)
  const [confirmDel, setConfirmDel] = useState(null)  // fqdn a confirmar
  const [page, setPage] = useState(0)                  // paginação (10 por página)
  const PER_PAGE = 10

  const load = () => {
    setLoading(true)
    api.manualSiteList()
      .then((r) => setSites(Array.isArray(r) ? r : []))
      .catch((e) => setMsg({ ok: false, text: e.message }))
      .finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])

  const remove = async (s) => {
    setMsg(null)
    try {
      await api.manualSiteDelete(s.domain, s.slug)
      setConfirmDel(null)
      setMsg({ ok: true, text: `Site ${s.fqdn} removido.` })
      load()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  const shown = sites.filter((s) => {
    if (filter !== "all" && (s.source || "run") !== filter) return false
    if (q.trim()) {
      const t = `${s.razao_social} ${s.cnpj} ${s.fqdn} ${s.cidade}`.toLowerCase()
      if (!t.includes(q.trim().toLowerCase())) return false
    }
    return true
  })
  const counts = {
    all: sites.length,
    manual: sites.filter((s) => (s.source || "run") === "manual").length,
    run: sites.filter((s) => (s.source || "run") === "run").length,
  }
  const fmtDate = (ts) => {
    if (!ts) return "—"
    try { return new Date(ts * 1000).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) }
    catch { return "—" }
  }

  // Paginação: 10 por página. Reseta pra página 1 quando muda filtro/busca.
  useEffect(() => { setPage(0) }, [q, filter])
  const totalPages = Math.max(1, Math.ceil(shown.length / PER_PAGE))
  const pageSafe = Math.min(page, totalPages - 1)
  const pageItems = shown.slice(pageSafe * PER_PAGE, pageSafe * PER_PAGE + PER_PAGE)

  const FilterBtn = ({ id, label }) => (
    <button onClick={() => setFilter(id)}
      className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition ${filter === id ? "bg-lime-500 text-neutral-900" : "bg-neutral-100 dark:bg-neutral-800 text-neutral-600 dark:text-neutral-300 hover:bg-neutral-200 dark:hover:bg-neutral-700"}`}>
      {label} <span className="opacity-70">({counts[id] ?? 0})</span>
    </button>
  )

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="flex gap-2">
          <FilterBtn id="all" label="Todos" />
          <FilterBtn id="manual" label="🟢 Manual" />
          <FilterBtn id="run" label="🔵 Run" />
        </div>
        <div className="flex-1 min-w-[200px]">
          <input className={inputCls} placeholder="Buscar por nome, CNPJ, cidade ou domínio…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button onClick={load} className="rounded-lg border-2 border-neutral-200 dark:border-neutral-700 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800 flex items-center gap-2">
          <RefreshCw size={15} /> Atualizar
        </button>
      </div>

      {msg && (
        <div className={`mb-4 p-3 rounded-lg text-sm font-medium ${msg.ok ? "bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-200" : "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300"}`}>{msg.text}</div>
      )}

      {loading ? (
        <Card><p className="text-sm text-neutral-500">Carregando sites…</p></Card>
      ) : shown.length === 0 ? (
        <Card className="text-center py-10">
          <LayoutGrid size={36} className="mx-auto mb-3 text-neutral-300 dark:text-neutral-600" />
          <p className="text-sm text-neutral-500">Nenhum site {filter !== "all" ? `(${filter})` : ""} por aqui ainda.</p>
        </Card>
      ) : (
        <>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {pageItems.map((s) => {
            const manual = (s.source || "run") === "manual"
            return (
              <Card key={`${s.domain}/${s.slug}`} className="border-2 border-neutral-200 dark:border-neutral-800 hover:shadow-md transition-shadow">
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div className="min-w-0">
                    <h4 className="font-bold text-sm truncate" title={s.razao_social || s.slug}>{s.razao_social || s.slug || "—"}</h4>
                    <span className="text-xs text-neutral-500 font-mono">/{s.slug}</span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {manual
                      ? <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-lime-100 dark:bg-lime-900/40 text-lime-700 dark:text-lime-300 flex items-center gap-1"><Hand size={10} /> Manual</span>
                      : <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 flex items-center gap-1"><Rocket size={10} /> Run</span>}
                    {s.verified && <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300">✓ metatag</span>}
                  </div>
                </div>

                <div className="flex items-center gap-1.5 mb-3 min-w-0">
                  <a href={s.url} target="_blank" rel="noreferrer" className="text-sm text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1 truncate">
                    <Globe size={13} className="shrink-0" /> <span className="truncate">{s.fqdn}</span>
                  </a>
                  <CopyButton text={s.fqdn} label={<Copy size={12} />} className="shrink-0 text-neutral-400 hover:text-emerald-500 dark:hover:text-emerald-300 text-[10px]" />
                </div>

                <div className="grid grid-cols-1 gap-y-1.5 mb-3">
                  <InfoRow label="Razão social" value={s.razao_social} />
                  <InfoRow label="CNPJ" value={fmtCnpj(s.cnpj)} copy={s.cnpj} />
                  <InfoRow label="Email" value={s.email} />
                  <InfoRow label="Telefone" value={s.telefone} />
                  <InfoRow label="Domínio" value={s.fqdn} />
                  <InfoRow label="Logradouro" value={[s.logradouro, s.numero].filter(Boolean).join(", ").toUpperCase()} />
                  <InfoRow label="Bairro" value={s.bairro} />
                  <InfoRow label="Cidade" value={[s.cidade, s.uf].filter(Boolean).join(" - ")} />
                  <InfoRow label="CEP" value={s.cep} />
                </div>

                <div className="flex items-center justify-between gap-2 pt-2 border-t border-neutral-100 dark:border-neutral-800">
                  <span className="text-[11px] text-neutral-400">Atualizado {fmtDate(s.updated_at)}</span>
                  <div className="flex items-center gap-1.5">
                    <button onClick={() => onEdit(s)} className={`${btnPrimary} px-3 py-1.5 text-xs flex items-center gap-1`}>
                      <Pencil size={12} /> Editar
                    </button>
                    {confirmDel === s.fqdn ? (
                      <>
                        <button onClick={() => remove(s)} className="text-xs font-bold text-red-600 hover:underline px-1">Sim</button>
                        <button onClick={() => setConfirmDel(null)} className="text-xs text-neutral-500 hover:underline px-1">não</button>
                      </>
                    ) : (
                      <button onClick={() => setConfirmDel(s.fqdn)} title="Excluir" className="p-1.5 rounded-lg text-neutral-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/30">
                        <Trash2 size={14} />
                      </button>
                    )}
                  </div>
                </div>
              </Card>
            )
          })}
        </div>

        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 mt-6">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={pageSafe === 0}
              className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={16} /> Anteriores
            </button>
            <div className="flex items-center gap-1.5 px-2">
              {Array.from({ length: totalPages }).map((_, i) => (
                <button
                  key={i} onClick={() => setPage(i)}
                  className={`h-8 min-w-8 px-2 rounded-lg text-sm font-bold tabular-nums transition ${
                    i === pageSafe
                      ? "cyber-btn"
                      : "border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/70 hover:bg-neutral-100 dark:hover:bg-emerald-400/10"
                  }`}
                >
                  {i + 1}
                </button>
              ))}
            </div>
            <button
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))} disabled={pageSafe >= totalPages - 1}
              className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Próximos <ChevronRight size={16} />
            </button>
          </div>
        )}
        <p className="text-center text-xs text-neutral-400 dark:text-emerald-200/40 mt-2">
          Mostrando <b className="tabular-nums">{pageItems.length}</b> de <b className="tabular-nums">{shown.length}</b> sites
          {totalPages > 1 && <> · página <b className="tabular-nums">{pageSafe + 1}</b>/<b className="tabular-nums">{totalPages}</b></>}
        </p>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// PDFs / CNPJs — comprovantes gerados nas runs, com preview + edição de
// email/telefone comercial (salva em tempo real, re-renderiza) + download.
// ---------------------------------------------------------------------------
// Gerar um comprovante CNPJ manualmente (sem depender de run).
function ManualComprovante({ onGenerated }) {
  const [cnpj, setCnpj] = useState("")
  const [email, setEmail] = useState("")
  const [tel, setTel] = useState("")
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const digits = (cnpj || "").replace(/\D/g, "")
  const gen = async () => {
    if (digits.length !== 14) { setMsg({ ok: false, text: "CNPJ precisa ter 14 dígitos." }); return }
    setBusy(true); setMsg(null)
    try {
      const r = await api.comprovanteGenerate({
        cnpj: digits,
        email: email.trim() ? email.trim().toUpperCase() : null,
        telefone: tel.trim() || null,
      })
      setMsg({ ok: true, text: `Comprovante de ${r.card?.razao_social || digits} gerado!` })
      setCnpj(""); setEmail(""); setTel("")
      onGenerated && onGenerated()
    } catch (e) { setMsg({ ok: false, text: e.message }) } finally { setBusy(false) }
  }
  return (
    <Card className="mb-4 border-2 border-amber-300/40 dark:border-emerald-400/25">
      <div className="flex items-center gap-2 mb-3">
        <PlusCircle size={18} className="text-emerald-500" />
        <h4 className="font-display font-extrabold uppercase tracking-wide text-sm cyber-gold">Gerar comprovante manual</h4>
      </div>
      <p className="text-[11px] text-neutral-500 dark:text-emerald-200/50 mb-3">Digite um CNPJ e gere o PDF do comprovante na hora — sem precisar rodar uma run. Ele aparece na lista abaixo pra baixar/editar. Email e telefone são opcionais.</p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-2">
        <div className="sm:col-span-1">
          <label className="text-[11px] text-neutral-400">CNPJ</label>
          <input className={inputCls} placeholder="00.000.000/0000-00" value={cnpj} onChange={(e) => setCnpj(fmtCnpj(e.target.value.replace(/\D/g, "").slice(0, 14)))} onKeyDown={(e) => e.key === "Enter" && gen()} />
        </div>
        <div>
          <label className="text-[11px] text-neutral-400">Email (opcional)</label>
          <input className={inputCls} placeholder="EMAIL@EXEMPLO.COM" value={email} onChange={(e) => setEmail(e.target.value.toUpperCase())} />
        </div>
        <div>
          <label className="text-[11px] text-neutral-400">Telefone (opcional)</label>
          <input className={inputCls} placeholder="(11) 98212-0535" value={tel} onChange={(e) => setTel(e.target.value)} />
        </div>
      </div>
      <div className="flex items-center gap-3">
        <button className={`${btnPrimary} px-4 py-2 text-sm flex items-center gap-2`} disabled={busy || digits.length !== 14} onClick={gen}>
          {busy ? <RefreshCw size={14} className="animate-spin" /> : <FileText size={14} />} {busy ? "Gerando…" : "Gerar PDF"}
        </button>
        {msg && <span className={`text-xs ${msg.ok ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>{msg.text}</span>}
      </div>
    </Card>
  )
}

function PdfsCnpjs() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState("")
  const [page, setPage] = useState(0)
  const PER = 8

  const load = () => {
    setLoading(true)
    api.comprovantesList().then((r) => setItems(Array.isArray(r) ? r : [])).catch(() => {}).finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])
  useEffect(() => { setPage(0) }, [q])

  const shown = items.filter((c) => {
    if (!q.trim()) return true
    const t = `${c.razao_social} ${c.cnpj} ${c.telefone} ${c.email}`.toLowerCase()
    return t.includes(q.trim().toLowerCase())
  })
  const totalPages = Math.max(1, Math.ceil(shown.length / PER))
  const pageSafe = Math.min(page, totalPages - 1)
  const pageItems = shown.slice(pageSafe * PER, pageSafe * PER + PER)

  return (
    <div>
      <ManualComprovante onGenerated={load} />
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="flex-1 min-w-[220px]">
          <input className={inputCls} placeholder="Buscar por razão social, CNPJ, telefone ou email…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button onClick={load} className="rounded-lg border-2 border-neutral-200 dark:border-emerald-400/25 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10 flex items-center gap-2">
          <RefreshCw size={15} /> Atualizar
        </button>
      </div>

      {loading ? (
        <Card><p className="text-sm text-neutral-500">Carregando PDFs…</p></Card>
      ) : shown.length === 0 ? (
        <Card className="text-center py-10">
          <FileText size={36} className="mx-auto mb-3 text-neutral-300 dark:text-neutral-600" />
          <p className="text-sm text-neutral-500">Nenhum comprovante ainda. Eles aparecem aqui quando uma run gera o PDF do CNPJ.</p>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {pageItems.map((c) => <PdfCard key={c.id} card={c} />)}
          </div>
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2 mt-6">
              <button onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={pageSafe === 0}
                className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10 disabled:opacity-40 disabled:cursor-not-allowed">
                <ChevronLeft size={16} /> Anteriores
              </button>
              <span className="text-xs text-neutral-500 dark:text-emerald-200/60">
                Página <b className="tabular-nums">{pageSafe + 1}</b>/<b className="tabular-nums">{totalPages}</b> · <b className="tabular-nums">{shown.length}</b> PDFs
              </span>
              <button onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))} disabled={pageSafe >= totalPages - 1}
                className="inline-flex items-center gap-1 rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10 disabled:opacity-40 disabled:cursor-not-allowed">
                Próximos <ChevronRight size={16} />
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function PdfCard({ card }) {
  const [email, setEmail] = useState((card.email || "").toUpperCase())
  const [tel, setTel] = useState(fmtPhone(card.telefone || ""))
  const [showEdited, setShowEdited] = useState(!!card.edited)
  const [hasEdited, setHasEdited] = useState(!!card.edited)
  const [previewUrl, setPreviewUrl] = useState(null)
  const [ver, setVer] = useState(0) // incrementa a cada save → força recarregar preview
  const [save, setSave] = useState(null) // 'saving' | 'saved' | 'err'
  const [errMsg, setErrMsg] = useState("")
  const editavel = !!card.cnpj

  const doSave = async () => {
    if (!editavel) return
    setSave("saving"); setErrMsg("")
    try {
      await api.comprovanteEdit(card.id, { email, telefone: tel, cnpj: card.cnpj || null })
      setSave("saved"); setHasEdited(true); setShowEdited(true)
      setVer((v) => v + 1)  // sempre muda → o preview re-busca (mesmo no 2º save)
    } catch (e) {
      setSave("err"); setErrMsg(e.message || "erro ao salvar")
    }
  }

  // Preview (blob autenticado) — recarrega quando alterna original/editado.
  useEffect(() => {
    let alive = true; let url = null
    setPreviewUrl(null)
    api.comprovanteBlobUrl(card.id, showEdited).then((u) => { if (alive) { url = u; setPreviewUrl(u) } }).catch(() => {})
    return () => { alive = false; if (url) URL.revokeObjectURL(url) }
  }, [card.id, showEdited, ver])

  const download = async (edited) => {
    try {
      const u = await api.comprovanteBlobUrl(card.id, edited)
      const a = document.createElement("a"); a.href = u; a.download = card.id; a.click()
      setTimeout(() => URL.revokeObjectURL(u), 5000)
    } catch { /* ignore */ }
  }

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="font-bold text-sm truncate" title={card.razao_social}>{card.razao_social}</h4>
          {card.cnpj && (
            <div className="flex items-center gap-1.5 mt-0.5">
              <span className="font-mono text-xs font-bold text-neutral-800 dark:text-emerald-100">{fmtCnpj(card.cnpj)}</span>
              <CopyButton text={card.cnpj} label={<Copy size={11} />} className="shrink-0 text-neutral-400 hover:text-emerald-500 dark:hover:text-emerald-300 text-[10px]" />
            </div>
          )}
          <span className="text-[11px] text-neutral-500 dark:text-emerald-200/50">{card.data || "—"}{card.phone_sms ? ` · SMS ${card.phone_sms}` : ""}</span>
        </div>
        <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-500/15 text-blue-700 dark:text-blue-300 shrink-0 flex items-center gap-1"><Rocket size={10} /> Run</span>
      </div>

      {/* Preview do PDF */}
      <div className="relative rounded-lg overflow-hidden border border-neutral-200 dark:border-emerald-400/20 bg-neutral-50 dark:bg-[#04120c]" style={{ height: 260 }}>
        {previewUrl
          ? <iframe title={card.id} src={`${previewUrl}#toolbar=0&navpanes=0`} className="w-full h-full" />
          : <div className="w-full h-full grid place-items-center text-xs text-neutral-400">carregando preview…</div>}
        {hasEdited && (
          <div className="absolute top-1.5 right-1.5 flex gap-1">
            <button onClick={() => setShowEdited(false)} className={`text-[10px] px-2 py-0.5 rounded ${!showEdited ? "cyber-btn" : "bg-black/40 text-white"}`}>Original</button>
            <button onClick={() => setShowEdited(true)} className={`text-[10px] px-2 py-0.5 rounded ${showEdited ? "cyber-btn" : "bg-black/40 text-white"}`}>Editado</button>
          </div>
        )}
      </div>

      {/* Campos editáveis: email + telefone comercial */}
      <div className="grid grid-cols-1 gap-2">
        <label className="text-[10px] uppercase tracking-wide text-neutral-400 dark:text-emerald-300/50 flex flex-col gap-1">Email comercial
          <input className={`${inputCls} py-1.5 uppercase`} placeholder={editavel ? "EMAIL@EMPRESA.COM" : "informe o CNPJ acima p/ editar"} value={email} onChange={(e) => setEmail(e.target.value.toUpperCase())} disabled={!editavel} />
        </label>
        <label className="text-[10px] uppercase tracking-wide text-neutral-400 dark:text-emerald-300/50 flex flex-col gap-1">Telefone comercial
          <input className={`${inputCls} py-1.5`} placeholder={editavel ? "(11) 98212-0535" : "informe o CNPJ acima p/ editar"} value={tel} onChange={(e) => setTel(fmtPhone(e.target.value))} disabled={!editavel} />
        </label>
      </div>

      <div className="flex items-center justify-between gap-2 pt-2 border-t border-neutral-100 dark:border-emerald-400/10">
        <span className="text-[11px] min-w-0 truncate" title={errMsg}>
          {save === "saving" && <span className="text-amber-500">salvando…</span>}
          {save === "saved" && <span className="text-emerald-600 dark:text-emerald-400">✓ salvo</span>}
          {save === "err" && <span className="text-red-500 truncate">⚠ {errMsg || "erro ao salvar"}</span>}
        </span>
        <div className="flex items-center gap-1.5 shrink-0">
          <button onClick={doSave} disabled={!editavel || save === "saving"} className={`${btnPrimary} px-3 py-1.5 text-xs flex items-center gap-1 disabled:opacity-50`}>
            <Save size={12} /> {save === "saving" ? "Salvando…" : "Salvar"}
          </button>
          {hasEdited && (
            <button onClick={() => download(true)} className="rounded-lg border border-emerald-300 dark:border-emerald-400/40 text-emerald-700 dark:text-emerald-300 px-3 py-1.5 text-xs font-medium hover:bg-emerald-50 dark:hover:bg-emerald-400/10 flex items-center gap-1">
              <Download size={12} /> Editado
            </button>
          )}
          <button onClick={() => download(false)} className="rounded-lg border border-neutral-300 dark:border-emerald-400/25 text-neutral-600 dark:text-emerald-100/80 px-3 py-1.5 text-xs font-medium hover:bg-neutral-100 dark:hover:bg-emerald-400/10 flex items-center gap-1">
            <Download size={12} /> Original
          </button>
        </div>
      </div>
    </Card>
  )
}

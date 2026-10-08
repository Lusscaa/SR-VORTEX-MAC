// Cliente HTTP fino pro backend FastAPI (proxied em /api pelo Vite).
//
// Toda chamada injeta `Authorization: Bearer <token>` (token guardado no
// localStorage). Em 401 o token é descartado e um evento global avisa a UI
// para voltar ao login.

const TOKEN_KEY = 'panel_token'

export const auth = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
  isAuthed: () => Boolean(localStorage.getItem(TOKEN_KEY)),
}

// Login não passa por http() (não exige token e não deve disparar o evento de
// logout em 401 — credencial errada é tratada na própria tela).
export async function login(username, password) {
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    let detail = 'Usuário ou senha inválidos'
    try { detail = (await res.json()).detail || detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  const { token } = await res.json()
  auth.set(token)
  return token
}

export function logout() {
  auth.clear()
  window.dispatchEvent(new Event('panel-unauthorized'))
}

async function http(path, opts = {}) {
  const token = auth.get()
  const headers = { ...(opts.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  // timeoutMs: aborta a chamada em vez de deixar o botão girando pra sempre
  // quando o backend fica pendurado (ex.: Netlify sem resposta).
  const { timeoutMs, ...rest } = opts
  let signal, timer
  if (timeoutMs) {
    const ctrl = new AbortController()
    signal = ctrl.signal
    timer = setTimeout(() => ctrl.abort(), timeoutMs)
  }
  let res
  try {
    res = await fetch(`/api${path}`, { ...rest, headers, signal })
  } catch (e) {
    if (e?.name === 'AbortError') {
      throw new Error(
        `A operação passou de ${Math.round(timeoutMs / 1000)}s sem resposta do servidor — ` +
        'provavelmente a Netlify não respondeu. Veja o log do backend e tente de novo.'
      )
    }
    throw e
  } finally {
    if (timer) clearTimeout(timer)
  }
  if (res.status === 401) {
    // Token ausente/expirado: limpa e avisa a UI para redirecionar ao login.
    auth.clear()
    window.dispatchEvent(new Event('panel-unauthorized'))
    throw new Error('Sessão expirada — faça login novamente.')
  }
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  stats: (period = 'total', date = '') => {
    const params = new URLSearchParams()
    if (date) params.append('date', date)
    else params.append('period', period)
    return http(`/stats?${params.toString()}`)
  },
  runs: (status) => http(`/runs${status ? `?status=${status}` : ''}`),
  runDetail: (id) => http(`/runs/${id}`),
  runLog: (id) => http(`/runs/${id}/log`),
  retryRun: (id) => http(`/runs/${id}/retry`, { method: 'POST' }),
  rerunSame: (id) => http(`/runs/${id}/rerun-same`, { method: 'POST' }),
  continuarBm: (id) => http(`/runs/${id}/continuar-bm`, { method: 'POST' }),
  terminateRun: (id) => http(`/runs/${id}/terminate`, { method: 'POST' }),
  releaseAccountRun: (id) => http(`/runs/${id}/release-account`, { method: 'POST' }),
  recoverAccountCnpj: (id) => http(`/runs/${id}/recover-account-cnpj`, { method: 'POST' }),
  rescueAccount: (email) => http('/pools/accounts/rescue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  }),
  discardAccount: (email) => http('/pools/accounts/discard', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  }),
  assumeRun: (id) => http(`/runs/${id}/assume`, { method: 'POST' }),
  resumeRun: (id, nextStep = null) => http(`/runs/${id}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ next_step: nextStep }),
  }),
  restartFromStep: (id) => http(`/runs/${id}/restart-from-step`, { method: 'POST' }),
  restartToBusinessDetails: (id) => http(`/runs/${id}/restart-to-business-details`, { method: 'POST' }),
  bms: (tier) => http(`/bms${tier ? `?tier=${tier}` : ''}`),
  refreshBms: () => http('/bms/refresh', { method: 'POST' }),
  refreshBmsStatus: () => http('/bms/refresh-status'),
  setBmStatus: (businessId, status) => http(`/bms/${businessId}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  }),
  pools: () => http('/pools'),
  cnpjInfo: (cnpj) => http(`/cnpj/${encodeURIComponent(cnpj)}`),
  uploadCnpjs: (file) => {
    const fd = new FormData(); fd.append('file', file)
    return http('/pools/cnpjs', { method: 'POST', body: fd })
  },
  searchCnpjs: (filters) => http('/cnpjs/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(filters),
  }),
  addCnpjsManual: (text) => http('/pools/cnpjs/manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }),
  uploadAccounts: (file) => {
    const fd = new FormData(); fd.append('file', file)
    return http('/pools/accounts', { method: 'POST', body: fd })
  },
  addAccountsManual: (text) => http('/pools/accounts/manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }),
  uploadProxies: (file, scheme) => {
    const fd = new FormData(); fd.append('file', file)
    if (scheme) fd.append('scheme', scheme)
    return http('/pools/proxies', { method: 'POST', body: fd })
  },
  addProxiesManual: (text, scheme) => http('/pools/proxies/manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, scheme }),
  }),
  deleteCnpjs: (values) => http('/pools/cnpjs/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values }),
  }),
  deleteAccounts: (values) => http('/pools/accounts/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values }),
  }),
  deleteProxies: (values) => http('/pools/proxies/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values }),
  }),
  enqueue: (count, useProxy = true, proxyType = 'datacenter', smsOption = null, smsDdd = null, smsRushServer = null, smsFallbackOption = null, dualBm = false, browserProvider = 'adspower', wabaEnabled = false) => http('/jobs/enqueue', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ count, use_proxy: useProxy, proxy_type: proxyType, sms_option: smsOption, sms_ddd: smsDdd || null, smsrush_server: smsRushServer ? Number(smsRushServer) : null, sms_fallback_option: smsFallbackOption || null, dual_bm: dualBm, browser_provider: browserProvider, waba_enabled: wabaEnabled }),
  }),
  loginOnly: (count, useProxy = true) => http('/jobs/login-only', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ count, use_proxy: useProxy }),
  }),
  setConcurrency: (n) => http('/jobs/concurrency', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ n }),
  }),
  manualRun: (payload) => http('/run/manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  settings: () => http('/settings'),
  saveSettings: (payload) => http('/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  // Aba "APIs" — editor visual das chaves do .env (SMS, captcha, CNPJ, etc.).
  getApis: () => http('/apis'),
  saveApis: (values) => http('/apis', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values }),
  }),
  // Apaga TODAS as chaves de serviço (p/ entregar o bot sem vazar saldo).
  clearApis: () => http('/apis/clear', { method: 'POST' }),
  // Testa cada API ao vivo (saldo SMS/captcha, ping AdsPower) e diz o que falta.
  checkApis: () => http('/apis/check', { method: 'POST' }),
  // Healthcheck público — usado pra saber quando o backend voltou após reiniciar.
  health: () => http('/health'),
  // Reinicia o backend (recarrega o .env — ex.: nova sessão do SMS).
  restartBackend: () => http('/system/restart', { method: 'POST' }),
  // Netlify — domínios (host atual; cada CNPJ vira um subdomínio)
  netlifyUsage: () => http('/netlify/usage'),
  netlifyDomains: () => http('/netlify/domains'),
  addNetlifyDomain: (domain, api_token, cap = 10) => http('/netlify/domains', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ domain, api_token, cap }),
  }),
  deleteNetlifyDomain: (domain) => http(`/netlify/domains/${encodeURIComponent(domain)}`, { method: 'DELETE' }),
  resetNetlifyDomain: (domain) => http(`/netlify/domains/${encodeURIComponent(domain)}/reset`, { method: 'POST' }),
  setNetlifyDomainQuota: (domain, remaining, cap) => http(`/netlify/domains/${encodeURIComponent(domain)}/quota`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ remaining, cap }),
  }),
  netlifyDomainStatus: (domain) => http(`/netlify/domains/${encodeURIComponent(domain)}/status`),
  // Provedor de hospedagem (netlify padrão | cloudflare — teste)
  hostingProvider: () => http('/hosting/provider'),
  setHostingProvider: (provider) => http('/hosting/provider', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider }),
  }),
  // Cloudflare — domínios (host alternativo; mesmo modelo de subdomínio por CNPJ)
  cloudflareDomains: () => http('/cloudflare/domains'),
  addCloudflareDomain: (domain, account_id, api_token, cap = 100) => http('/cloudflare/domains', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ domain, account_id, api_token, cap }),
  }),
  deleteCloudflareDomain: (domain) => http(`/cloudflare/domains/${encodeURIComponent(domain)}`, { method: 'DELETE' }),
  resetCloudflareDomain: (domain) => http(`/cloudflare/domains/${encodeURIComponent(domain)}/reset`, { method: 'POST' }),
  setCloudflareDomainQuota: (domain, remaining, cap) => http(`/cloudflare/domains/${encodeURIComponent(domain)}/quota`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ remaining, cap }),
  }),
  cloudflareDomainStatus: (domain) => http(`/cloudflare/domains/${encodeURIComponent(domain)}/status`),
  exportAccounts: () => http('/pools/accounts/export'),
  resetWrongPasswordAccount: (email) => http('/pools/accounts/reset-wrong-password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  }),
  smsSessionStatus: () => http('/sms/session-status'),
  proxyHealthCheck: () => http('/proxies/health-check', { method: 'POST', timeoutMs: 120000 }),
  getDiscardedAccounts: () => http('/accounts/discarded'),
  getPendingDiscardAccounts: () => http('/accounts/pending-discard'),
  discardAccountsManual: (values) => http('/accounts/discard-manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values }),
  }),
  setAutopilot: (enabled, { runsPerCycle = 1, totalCycles = 3, useProxy = true, proxyType = 'datacenter', smsOption = null, smsDdd = null, smsrushServer = null, smsFallbackOption = null, dualBm = false, browserProvider = 'adspower', wabaEnabled = false } = {}) => http('/jobs/autopilot', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      enabled,
      runs_per_cycle: runsPerCycle,
      total_cycles: totalCycles,
      use_proxy: useProxy,
      proxy_type: proxyType,
      sms_option: smsOption,
      sms_ddd: smsDdd,
      smsrush_server: smsrushServer,
      sms_fallback_option: smsFallbackOption,
      dual_bm: dualBm,
      browser_provider: browserProvider,
      waba_enabled: wabaEnabled,
    }),
  }),
  // Aba "Gerar Site" — publicação manual do site de um CNPJ + metatag do Meta.
  manualSitePreview: (cnpj) => http(`/manual-site/preview/${encodeURIComponent(cnpj)}`),
  // payload: { cnpj, slug?, metatag?, domain?, fields{} } — serve p/ Publicar E Atualizar
  manualSitePublish: (payload) => http('/manual-site/publish', {
    method: 'POST', timeoutMs: 240000, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  manualSiteDelete: (domain, slug) => http('/manual-site/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ domain, slug }),
  }),
  manualSiteMetatag: (metatag, cnpj, fqdn) => http('/manual-site/metatag', {
    method: 'POST', timeoutMs: 240000, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ metatag, cnpj: cnpj || null, fqdn: fqdn || null }),
  }),
  manualSiteList: () => http('/manual-site/list'),
  manualSiteCheck: (fqdn, token) => http('/manual-site/check', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fqdn, token: token || null }),
  }),

  // PDFs / CNPJs (comprovantes gerados nas runs).
  comprovantesList: () => http('/comprovantes'),
  comprovanteGenerate: (payload) => http('/comprovantes/generate', {
    method: 'POST', timeoutMs: 120000, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  comprovanteEdit: (id, payload) => http(`/comprovantes/${encodeURIComponent(id)}/edit`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  // CRM (WhatsApp Cloud API) — conectar BMs, templates, disparos, leads, chat.
  crmBms: () => http('/crm/bms'),
  crmAddManualBm: (payload) => http('/crm/bms/manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmSetToken: (id, token, durable = true) => http(`/crm/bms/${encodeURIComponent(id)}/token`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, durable }),
  }),
  crmPhones: (id) => http(`/crm/bms/${encodeURIComponent(id)}/phones`),
  crmConnect: (id, payload) => http(`/crm/bms/${encodeURIComponent(id)}/connect`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmDisconnect: (id) => http(`/crm/bms/${encodeURIComponent(id)}/disconnect`, { method: 'POST' }),
  crmWebhookSubStatus: (id) => http(`/crm/bms/${encodeURIComponent(id)}/webhook-subscription`),
  crmSubscribeWebhook: (id) => http(`/crm/bms/${encodeURIComponent(id)}/subscribe-webhook`, { method: 'POST' }),
  crmTemplates: (id) => http(`/crm/bms/${encodeURIComponent(id)}/templates`),
  crmCreateTemplate: (payload) => http('/crm/templates/create', {
    method: 'POST', timeoutMs: 120000, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmLeads: (tag = '', search = '') =>
    http(`/crm/leads?tag=${encodeURIComponent(tag)}&search=${encodeURIComponent(search)}`),
  crmAddLeads: (payload) => http('/crm/leads', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmUpdateLead: (id, payload) => http(`/crm/leads/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmDeleteLeads: (ids) => http('/crm/leads/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  }),
  crmDispatch: (payload) => http('/crm/dispatch', {
    method: 'POST', timeoutMs: 600000, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmDispatchMulti: (payload) => http('/crm/dispatch-multi', {
    method: 'POST', timeoutMs: 60000, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  crmDispatchStatus: (jobId) => http(`/crm/dispatch/${encodeURIComponent(jobId)}`),
  crmTokenCheck: (id) => http(`/crm/bms/${encodeURIComponent(id)}/token-check`),
  // Registro de número na BM (após adicionado ao WABA)
  crmPhoneStatus: (id, pnid) => http(`/crm/bms/${encodeURIComponent(id)}/phones/${encodeURIComponent(pnid)}/status`),
  crmRequestCode: (id, pnid, payload) => http(`/crm/bms/${encodeURIComponent(id)}/phones/${encodeURIComponent(pnid)}/request-code`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }),
  crmVerifyCode: (id, pnid, code) => http(`/crm/bms/${encodeURIComponent(id)}/phones/${encodeURIComponent(pnid)}/verify-code`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
  }),
  crmRegisterPhone: (id, pnid, pin) => http(`/crm/bms/${encodeURIComponent(id)}/phones/${encodeURIComponent(pnid)}/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }),
  }),
  // Webhook (respostas dos leads)
  crmWebhookConfig: () => http('/crm/webhook-config'),
  crmSetWebhookConfig: (payload) => http('/crm/webhook-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }),
  crmChats: () => http('/crm/chats'),
  crmChatMessages: (phone) => http(`/crm/chats/${encodeURIComponent(phone)}`),
  crmStats: () => http('/crm/stats'),

  // Baixa o PDF (com auth) como blob e devolve um objectURL p/ preview/download.
  comprovanteBlobUrl: async (id, edited = false) => {
    const token = auth.get()
    // _=<ts> quebra o cache do navegador (senão serve o PDF antigo após editar).
    const qs = `${edited ? 'edited=1&' : ''}_=${Date.now()}`
    const res = await fetch(`/api/comprovantes/${encodeURIComponent(id)}/file?${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: 'no-store',
    })
    if (!res.ok) throw new Error(`Falha ao carregar PDF (${res.status})`)
    return URL.createObjectURL(await res.blob())
  },
}

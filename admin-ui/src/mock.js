// Dados de exemplo p/ VITE_USE_MOCK=true. Cobrem 2 instâncias (2 sócios):
// uma online com runs variados, outra offline. Datas relativas ao "agora".
const now = Date.now()
const ago = (ms) => new Date(now - ms).toISOString()
const min = 60_000
const hour = 60 * min

const INSTANCES = [
  {
    instance_id: 'spo-01',
    instance_name: 'Sócio A — São Paulo',
    last_seen: ago(20_000),
    online: true,
    counts: { cnpjs: 42, fb_accounts: 6, proxies: 8, runs: 188 },
  },
  {
    instance_id: 'cwb-02',
    instance_name: 'Sócio B — Curitiba',
    last_seen: ago(3 * hour + 12 * min),
    online: false,
    counts: { cnpjs: 17, fb_accounts: 3, proxies: 4, runs: 51 },
  },
]

const RUNS = {
  'spo-01': [
    { run_id: 'r-9001', status: 'running',    cnpj: '11.222.333/0001-44', account: 'maria.silva@mail.com', started_at: ago(3 * min),  finished_at: null,            error: null },
    { run_id: 'r-9000', status: 'success',    cnpj: '22.333.444/0001-55', account: 'joao.souza@mail.com',  started_at: ago(38 * min), finished_at: ago(31 * min),   error: null },
    { run_id: 'r-8999', status: 'failed',     cnpj: '33.444.555/0001-66', account: 'ana.costa@mail.com',   started_at: ago(52 * min), finished_at: ago(49 * min),   error: 'SMS não chegou (timeout 120s)' },
    { run_id: 'r-8998', status: 'incomplete', cnpj: '44.555.666/0001-77', account: 'pedro.lima@mail.com',  started_at: ago(2 * hour), finished_at: ago(2 * hour - 6 * min), error: 'parou no passo 2 (editor)' },
    { run_id: 'r-8997', status: 'queued',     cnpj: '55.666.777/0001-88', account: 'lucia.melo@mail.com',  started_at: null,           finished_at: null,            error: null },
    { run_id: 'r-8996', status: 'success',    cnpj: '66.777.888/0001-99', account: 'carlos.dias@mail.com', started_at: ago(5 * hour), finished_at: ago(5 * hour - 8 * min), error: null },
  ],
  'cwb-02': [
    { run_id: 'r-5101', status: 'failed',  cnpj: '77.888.999/0001-00', account: 'rafa.alves@mail.com', started_at: ago(3 * hour + 30 * min), finished_at: ago(3 * hour + 28 * min), error: 'proxy 407 (IP fora da whitelist)' },
    { run_id: 'r-5100', status: 'success', cnpj: '88.999.000/0001-11', account: 'bia.rocha@mail.com',  started_at: ago(6 * hour),            finished_at: ago(6 * hour - 9 * min),  error: null },
  ],
}

const LOGS = {
  'r-9001': [
    { ts: ago(3 * min),       level: 'info',  msg: 'run iniciado · cnpj=11.222.333/0001-44 · perfil AdsPower db34c90' },
    { ts: ago(3 * min - 4e3), level: 'info',  msg: 'preflight: relógio OK, Cloudflare pronto, proxy huqcplys-BR-3 200' },
    { ts: ago(3 * min - 12e3), level: 'info', msg: 'login submetido, aguardando 2FA' },
    { ts: ago(3 * min - 28e3), level: 'warn', msg: 'TOTP aceito após 1 retry' },
    { ts: ago(3 * min - 40e3), level: 'info', msg: 'passo 1/5: dados da empresa preenchidos' },
    { ts: ago(2 * min),       level: 'info',  msg: 'passo 2/5: âncora "Detalhes da empresa" encontrada' },
    { ts: ago(80e3),          level: 'info',  msg: 'passo 3/5: domínio aceito' },
    { ts: ago(20e3),          level: 'info',  msg: 'aguardando criação da WABA…' },
  ],
  'r-8999': [
    { ts: ago(52 * min),       level: 'info',  msg: 'run iniciado · cnpj=33.444.555/0001-66' },
    { ts: ago(52 * min - 30e3), level: 'info', msg: 'login OK, telefone submetido p/ verificação' },
    { ts: ago(50 * min),       level: 'warn',  msg: 'SMS não recebido, retry 1/2' },
    { ts: ago(49 * min - 30e3), level: 'warn', msg: 'SMS não recebido, retry 2/2' },
    { ts: ago(49 * min),       level: 'error', msg: 'SMS não chegou (timeout 120s) — run abortado' },
  ],
  'r-8998': [
    { ts: ago(2 * hour),       level: 'info',  msg: 'run iniciado · cnpj=44.555.666/0001-77' },
    { ts: ago(2 * hour - 2 * min), level: 'info', msg: 'passo 1/5 OK' },
    { ts: ago(2 * hour - 5 * min), level: 'error', msg: 'passo 2: business_id=None — cascata de editor, fail-fast' },
  ],
}

function logFor(runId) {
  return LOGS[runId] || [
    { ts: ago(10 * min), level: 'info', msg: `run ${runId}: sem log detalhado capturado` },
  ]
}

const POOLS = {
  'spo-01': {
    cnpjs: [
      { cnpj: '11.222.333/0001-44', razao_social: 'ACME Comércio LTDA', status: 'em uso' },
      { cnpj: '22.333.444/0001-55', razao_social: 'Beta Serviços ME', status: 'livre' },
      { cnpj: '33.444.555/0001-66', razao_social: 'Gama Tech EIRELI', status: 'queimado' },
      { cnpj: '55.666.777/0001-88', razao_social: 'Delta Logística SA', status: 'livre' },
    ],
    fb_accounts: [
      { email: 'maria.silva@mail.com', password: 'M@r1a2026!', status: 'em uso', proxy: 'huqcplys-BR-3', twofa_secret: 'JBSWY3DPEHPK3PXP' },
      { email: 'joao.souza@mail.com',  password: 'J0aoS#2026', status: 'livre', proxy: 'huqcplys-BR-1', twofa_secret: 'KZXW6YTBOI======' },
      { email: 'ana.costa@mail.com',   password: 'An@C0sta77', status: 'bloqueada', proxy: 'huqcplys-BR-2', twofa_secret: 'MFRGGZDFMZTWQ===' },
    ],
    proxies: [
      { label: 'huqcplys-BR-1', host: 'p.webshare.io', port: 80, status: 'ok', last_check: ago(2 * min) },
      { label: 'huqcplys-BR-2', host: 'p.webshare.io', port: 80, status: 'ok', last_check: ago(4 * min) },
      { label: 'huqcplys-BR-3', host: 'p.webshare.io', port: 80, status: '407', last_check: ago(11 * min) },
    ],
  },
  'cwb-02': {
    cnpjs: [
      { cnpj: '77.888.999/0001-00', razao_social: 'Omega Varejo LTDA', status: 'queimado' },
      { cnpj: '88.999.000/0001-11', razao_social: 'Sigma Indústria SA', status: 'livre' },
    ],
    fb_accounts: [
      { email: 'rafa.alves@mail.com', password: 'R@fa2026xx', status: 'bloqueada', proxy: 'cwb-res-1', twofa_secret: 'NB2W45DFOIZA====' },
      { email: 'bia.rocha@mail.com',  password: 'B1aR0cha!!', status: 'livre',     proxy: 'cwb-res-2', twofa_secret: 'ORSXG5BAONSWG===' },
    ],
    proxies: [
      { label: 'cwb-res-1', host: 'p.webshare.io', port: 80, status: '407', last_check: ago(3 * hour) },
      { label: 'cwb-res-2', host: 'p.webshare.io', port: 80, status: 'ok',  last_check: ago(3 * hour + 10 * min) },
    ],
  },
}

export const mockDb = {
  login(username, password) {
    if (!username || !password) throw Object.assign(new Error('credenciais inválidas'), { status: 401 })
    return { token: 'mock-token-' + username }
  },
  instances() { return INSTANCES },
  runs(id, status) {
    const rows = RUNS[id] || []
    return status ? rows.filter((r) => r.status === status) : rows
  },
  runLog(id, runId) { return { lines: logFor(runId) } },
  pools(id) { return POOLS[id] || { cnpjs: [], fb_accounts: [], proxies: [] } },
}

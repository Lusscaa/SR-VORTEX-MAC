import { api } from '../api.js'
import { usePoll } from '../hooks.js'
import { timeAgo } from '../utils.js'
import { Badge, CopyButton, Card, Freshness, Secret, Skeleton, Table } from './ui.jsx'

// Tom dos status de recurso (CNPJ / conta / proxy): alarme p/ queimado/bloqueada/407.
function resTone(s) {
  const v = (s || '').toLowerCase()
  if (['queimado', 'bloqueada', '407', 'erro', 'banida'].includes(v)) return 'danger'
  if (['em uso', 'ok'].includes(v)) return 'green'
  if (['livre'].includes(v)) return 'neutral'
  return 'neutral'
}

export default function PoolsTab({ instanceId }) {
  const { data, error, lastOk, loading } = usePoll(
    () => api.getPools(instanceId),
    [instanceId],
    { intervalMs: 15000 },
  )

  if (loading && !data) {
    return <div className="space-y-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-40 w-full" />)}</div>
  }

  const cnpjs = data?.cnpjs || []
  const accounts = data?.fb_accounts || []
  const proxies = data?.proxies || []

  return (
    <div className="space-y-6">
      <div className="flex justify-end"><Freshness lastOk={lastOk} error={error} /></div>

      <Section title="CNPJs" count={cnpjs.length}>
        <Table
          rows={cnpjs}
          rowKey={(r) => r.cnpj}
          empty="Sem CNPJs nesta instância."
          columns={[
            { key: 'cnpj', label: 'CNPJ', render: (r) => (
              <span className="font-mono text-xs inline-flex items-center gap-1.5">{r.cnpj}<CopyButton text={r.cnpj} /></span>
            ) },
            { key: 'razao_social', label: 'Razão social' },
            { key: 'status', label: 'Status', render: (r) => <Badge tone={resTone(r.status)}>{r.status}</Badge> },
          ]}
        />
      </Section>

      <Section title="Contas Facebook" count={accounts.length} hint="credenciais — clique no olho p/ revelar">
        <Table
          rows={accounts}
          rowKey={(r) => r.email}
          empty="Sem contas nesta instância."
          columns={[
            { key: 'email', label: 'E-mail', render: (r) => (
              <span className="font-mono text-xs inline-flex items-center gap-1.5">{r.email}<CopyButton text={r.email} /></span>
            ) },
            { key: 'password', label: 'Senha', render: (r) => <Secret value={r.password} /> },
            { key: 'twofa_secret', label: '2FA (TOTP)', render: (r) => r.twofa_secret ? <Secret value={r.twofa_secret} /> : '—' },
            { key: 'proxy', label: 'Proxy', render: (r) => <span className="font-mono text-xs">{r.proxy || '—'}</span> },
            { key: 'status', label: 'Status', render: (r) => <Badge tone={resTone(r.status)}>{r.status}</Badge> },
          ]}
        />
      </Section>

      <Section title="Proxies" count={proxies.length}>
        <Table
          rows={proxies}
          rowKey={(r) => r.label}
          empty="Sem proxies nesta instância."
          columns={[
            { key: 'label', label: 'Rótulo', render: (r) => <span className="font-mono text-xs">{r.label}</span> },
            { key: 'host', label: 'Endereço', render: (r) => <span className="font-mono text-xs">{r.host}:{r.port}</span> },
            { key: 'status', label: 'Status', render: (r) => <Badge tone={resTone(r.status)}>{r.status}</Badge> },
            { key: 'last_check', label: 'Última checagem', render: (r) => timeAgo(r.last_check) },
          ]}
        />
      </Section>
    </div>
  )
}

function Section({ title, count, hint, children }) {
  return (
    <Card>
      <div className="mb-2 flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <span className="text-xs text-neutral-400 tabular-nums">{count}</span>
        {hint && <span className="ml-auto text-[11px] text-neutral-400">{hint}</span>}
      </div>
      {children}
    </Card>
  )
}

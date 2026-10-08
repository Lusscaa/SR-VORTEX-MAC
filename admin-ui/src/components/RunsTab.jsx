import { useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { api } from '../api.js'
import { usePoll } from '../hooks.js'
import { fmtTime } from '../utils.js'
import { Badge, Freshness, Skeleton, Table, statusTone } from './ui.jsx'
import LogViewer from './LogViewer.jsx'

const STATUSES = [
  { key: '', label: 'Todos' },
  { key: 'running', label: 'Rodando' },
  { key: 'success', label: 'Sucesso' },
  { key: 'failed', label: 'Falhou' },
  { key: 'incomplete', label: 'Incompleto' },
  { key: 'queued', label: 'Na fila' },
]

export default function RunsTab({ instanceId }) {
  const [status, setStatus] = useState('')
  const [selected, setSelected] = useState(null)

  // Filtra no servidor via ?status=; auto-refresh a cada 5s (Freshness mostra).
  const { data, error, lastOk, loading } = usePoll(
    () => api.listRuns(instanceId, status || undefined),
    [instanceId, status],
    { intervalMs: 5000 },
  )
  const rows = data || []

  const columns = useMemo(() => [
    { key: 'status', label: 'Status', render: (r) => <Badge tone={statusTone(r.status)}>{r.status}</Badge> },
    { key: 'run_id', label: 'Run', render: (r) => <span className="font-mono text-xs">{r.run_id}</span> },
    { key: 'cnpj', label: 'CNPJ', render: (r) => <span className="font-mono text-xs">{r.cnpj || '—'}</span> },
    { key: 'account', label: 'Conta', render: (r) => <span className="truncate max-w-[180px] inline-block align-bottom">{r.account || '—'}</span> },
    { key: 'started_at', label: 'Início', render: (r) => fmtTime(r.started_at) },
    { key: 'finished_at', label: 'Fim', render: (r) => fmtTime(r.finished_at) },
    {
      key: 'error', label: 'Erro',
      render: (r) => r.error
        ? <span className="text-red-600 dark:text-red-400 text-xs truncate max-w-[220px] inline-block align-bottom" title={r.error}>{r.error}</span>
        : '—',
    },
  ], [])

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1.5">
          {STATUSES.map((s) => (
            <button
              key={s.key}
              onClick={() => setStatus(s.key)}
              className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                status === s.key
                  ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                  : 'border border-neutral-300 dark:border-neutral-700 text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <Freshness lastOk={lastOk} error={error} />
      </div>

      {loading && !data
        ? <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
        : <Table columns={columns} rows={rows} rowKey={(r) => r.run_id} onRowClick={setSelected} empty="Nenhum run com esse filtro." />}

      {selected && (
        <RunDetail instanceId={instanceId} run={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  )
}

// Painel lateral (drawer) com os campos do run + visualizador de log.
function RunDetail({ instanceId, run, onClose }) {
  const live = run.status === 'running'
  return (
    <div className="fixed inset-0 z-30 flex">
      <div className="flex-1 bg-black/30" onClick={onClose} />
      <div className="w-full max-w-2xl h-full overflow-auto bg-white dark:bg-neutral-900 border-l border-neutral-200 dark:border-neutral-800 p-5 shadow-xl">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Badge tone={statusTone(run.status)}>{run.status}</Badge>
              <span className="font-mono text-sm">{run.run_id}</span>
            </div>
            <p className="mt-1 text-xs text-neutral-500">{run.cnpj} · {run.account}</p>
          </div>
          <button onClick={onClose} aria-label="Fechar" className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">
            <X size={18} />
          </button>
        </div>

        <dl className="mb-4 grid grid-cols-2 gap-3 text-sm">
          <Field label="Início" value={fmtTime(run.started_at)} />
          <Field label="Fim" value={fmtTime(run.finished_at)} />
          <Field label="CNPJ" value={run.cnpj} mono />
          <Field label="Conta" value={run.account} mono />
        </dl>

        {run.error && (
          <div className="mb-4 rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-sm text-red-700 dark:text-red-300">
            {run.error}
          </div>
        )}

        <LogViewer instanceId={instanceId} runId={run.run_id} live={live} />
      </div>
    </div>
  )
}

function Field({ label, value, mono }) {
  return (
    <div>
      <dt className="text-[11px] font-medium text-neutral-500 dark:text-neutral-400">{label}</dt>
      <dd className={`mt-0.5 ${mono ? 'font-mono text-xs' : ''}`}>{value || '—'}</dd>
    </div>
  )
}

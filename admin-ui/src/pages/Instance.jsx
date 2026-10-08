import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { api } from '../api.js'
import { usePoll } from '../hooks.js'
import { timeAgo } from '../utils.js'
import { Badge, Skeleton } from '../components/ui.jsx'
import InstanceSelector from '../components/InstanceSelector.jsx'
import RunsTab from '../components/RunsTab.jsx'
import PoolsTab from '../components/PoolsTab.jsx'

const TABS = [
  { key: 'runs', label: 'Runs' },
  { key: 'pools', label: 'Pools' },
]

export default function Instance() {
  const { id } = useParams()
  const nav = useNavigate()
  const [tab, setTab] = useState('runs')

  // Reusa a lista de instâncias p/ nome/status do cabeçalho + seletor de troca.
  const { data: instances } = usePoll(() => api.listInstances(), [], { intervalMs: 15000 })
  const current = instances?.find((i) => i.instance_id === id)

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <button onClick={() => nav('/instances')} aria-label="Voltar" className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">
            <ArrowLeft size={18} />
          </button>
          <div className="min-w-0">
            {current ? (
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-bold tracking-tight truncate">{current.instance_name}</h1>
                <Badge tone={current.online ? 'green' : 'neutral'}>{current.online ? 'online' : 'offline'}</Badge>
              </div>
            ) : (
              <Skeleton className="h-6 w-48" />
            )}
            <p className="text-xs text-neutral-400 font-mono">
              {id}{current && ` · contato ${timeAgo(current.last_seen)}`}
            </p>
          </div>
        </div>
        {instances && <InstanceSelector instances={instances} currentId={id} />}
      </div>

      <div className="mb-4 flex gap-1 border-b border-neutral-200 dark:border-neutral-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              tab === t.key
                ? 'border-neutral-900 dark:border-white text-neutral-900 dark:text-white'
                : 'border-transparent text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'runs' ? <RunsTab instanceId={id} /> : <PoolsTab instanceId={id} />}
    </div>
  )
}

import { useNavigate } from 'react-router-dom'
import { Server } from 'lucide-react'
import { api } from '../api.js'
import { usePoll } from '../hooks.js'
import { timeAgo } from '../utils.js'
import { Badge, Card, Freshness, PageHead, Skeleton, Stat } from '../components/ui.jsx'

export default function Overview() {
  const nav = useNavigate()
  const { data, error, lastOk, loading } = usePoll(
    () => api.listInstances(),
    [],
    { intervalMs: 10000 },
  )
  const instances = data || []

  return (
    <div>
      <PageHead title="Instâncias" subtitle="Cada instância é um sócio rodando localmente.">
        <Freshness lastOk={lastOk} error={error} />
      </PageHead>

      {loading && !data ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => <Skeleton key={i} className="h-44 w-full rounded-xl" />)}
        </div>
      ) : instances.length === 0 ? (
        <Card><p className="text-sm text-neutral-400 py-8 text-center">Nenhuma instância reportou ainda.</p></Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {instances.map((i) => (
            <Card key={i.instance_id} onClick={() => nav(`/instances/${i.instance_id}`)}>
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-neutral-100 dark:bg-neutral-800 text-neutral-500">
                    <Server size={15} />
                  </span>
                  <div className="min-w-0">
                    <h3 className="font-semibold truncate">{i.instance_name}</h3>
                    <p className="text-[11px] text-neutral-400 font-mono truncate">{i.instance_id}</p>
                  </div>
                </div>
                <Badge tone={i.online ? 'green' : 'neutral'}>{i.online ? 'online' : 'offline'}</Badge>
              </div>

              <div className="mt-4 grid grid-cols-2 gap-2">
                <Stat label="CNPJs" value={i.counts?.cnpjs ?? 0} />
                <Stat label="Contas FB" value={i.counts?.fb_accounts ?? 0} />
                <Stat label="Proxies" value={i.counts?.proxies ?? 0} />
                <Stat label="Runs" value={i.counts?.runs ?? 0} />
              </div>

              <p className="mt-3 text-xs text-neutral-400">Último contato: {timeAgo(i.last_seen)}</p>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

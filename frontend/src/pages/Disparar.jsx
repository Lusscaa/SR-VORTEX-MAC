import { useState } from 'react'
import { PageHead } from '../components/ui.jsx'
import Batch from './Batch.jsx'
import Manual from './Manual.jsx'

// Reúne os dois jeitos de disparar uma run: em LOTE (sobe pools e pareia N de
// uma vez) ou AVULSO (1 conta, teste/one-off). Antes eram duas abas; agora é um
// toggle só — é a mesma tarefa em duas escalas.
const TABS = [
  { key: 'lote', label: 'Lote', sub: 'Suba pools e dispare N runs em paralelo' },
  { key: 'avulso', label: 'Avulso', sub: 'Rode 1 conta (teste / one-off)' },
]

export default function Disparar() {
  const [mode, setMode] = useState('lote')
  const tab = TABS.find((t) => t.key === mode)
  return (
    <>
      <PageHead title="Disparar" subtitle={tab.sub}>
        <div className="inline-flex rounded-lg border border-neutral-200 dark:border-neutral-800 p-0.5">
          {TABS.map((t) => (
            <button
              key={t.key} type="button" onClick={() => setMode(t.key)}
              className={`rounded-md px-3.5 py-1.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60 ${
                mode === t.key
                  ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                  : 'text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-200'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </PageHead>
      {mode === 'lote' ? <Batch /> : <Manual />}
    </>
  )
}

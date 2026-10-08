import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronsUpDown, Check, Server } from 'lucide-react'

// Dropdown p/ trocar de instância sem voltar à visão geral.
export default function InstanceSelector({ instances, currentId }) {
  const nav = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const current = instances?.find((i) => i.instance_id === currentId)

  useEffect(() => {
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-2 rounded-lg border border-neutral-300 dark:border-neutral-700 px-3 py-1.5 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800 transition"
      >
        <Server size={14} className="text-neutral-400" />
        <span className="font-medium max-w-[200px] truncate">{current?.instance_name || 'Selecionar instância'}</span>
        <ChevronsUpDown size={14} className="text-neutral-400" />
      </button>

      {open && (
        <div className="absolute z-20 mt-1 w-72 rounded-lg border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 shadow-lg p-1">
          {instances?.map((i) => (
            <button
              key={i.instance_id}
              onClick={() => { setOpen(false); nav(`/instances/${i.instance_id}`) }}
              className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm text-left hover:bg-neutral-100 dark:hover:bg-neutral-800 transition"
            >
              <span className={`h-2 w-2 rounded-full shrink-0 ${i.online ? 'bg-lime-500' : 'bg-neutral-300 dark:bg-neutral-600'}`} />
              <span className="flex-1 truncate">{i.instance_name}</span>
              {i.instance_id === currentId && <Check size={14} className="text-neutral-400" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

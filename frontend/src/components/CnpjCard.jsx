import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, CopyButton } from './ui.jsx'

// Dados do CNPJ (BrasilAPI) agrupados em Identidade / Endereço / Contato.
// Dois modos:
//   • leitura (Lote): só campos preenchidos, esconde o que vem vazio.
//   • edição (Manual, `editable`): inputs controlados — o operador corrige o
//     que a Receita erra/omite ANTES do run. `onChange(fields)` sobe os valores
//     pro pai mandar no disparo. razao_social NUNCA é editável (string oficial).
// O "Número" ganha destaque âmbar quando a BrasilAPI devolve "S/N"/vazio — é o
// caso mais comum de dado faltante, e o que mais trava a verificação na Meta.

const isSN = (v) => !v || /^s\/?n$/i.test(String(v).trim())

// Literais explícitos: o JIT do Tailwind só detecta classes completas no
// código-fonte — `col-span-${n}` não geraria nada. Este mapa força os literais.
const SPAN = { 1: 'col-span-1', 2: 'col-span-2', 3: 'col-span-3', 4: 'col-span-4', 6: 'col-span-6' }

const BLANK = {
  nome_fantasia: '', logradouro: '', numero: '', complemento: '',
  bairro: '', cidade: '', uf: '', cep: '', telefone: '', email: '',
}

function SectionLabel({ children }) {
  return (
    <p className="col-span-6 text-[10px] font-semibold uppercase tracking-wider text-neutral-400 mt-3 first:mt-0">
      {children}
    </p>
  )
}

// Input controlado compacto. `warn` pinta a borda de âmbar e mostra a dica.
function EditCell({ label, value, onChange, span = 2, warn, hint, placeholder }) {
  const border = warn
    ? 'border-amber-400 focus:border-amber-500 dark:border-amber-500'
    : 'border-neutral-300 dark:border-neutral-700 focus:border-neutral-900 dark:focus:border-neutral-100'
  return (
    <label className={`text-xs ${SPAN[span]}`}>
      <span className="block text-neutral-400 mb-1">{label}</span>
      <input
        value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={`w-full rounded-lg bg-transparent border px-2.5 py-1.5 text-sm outline-none transition ${border}`}
      />
      {warn && hint && <span className="mt-1 block text-[10px] text-amber-600 dark:text-amber-400">{hint}</span>}
    </label>
  )
}

// Linha read-only: esconde quando vazia (a não ser que precise avisar, ex. nº).
function ReadCell({ label, value, span = 2, copy, warn }) {
  if (!value && !warn) return null
  return (
    <div className={`text-xs ${SPAN[span]}`}>
      <span className="block text-neutral-400 mb-0.5">{label}</span>
      <span className={`flex items-center gap-1.5 font-medium break-all ${warn ? 'text-amber-600 dark:text-amber-400' : ''}`}>
        {value || 'S/N'}
        {warn && <span title="A BrasilAPI não trouxe o número — confira na Receita / Infosimples.">⚠</span>}
        {copy && value && <CopyButton text={value} label="copiar" className="text-[10px] text-lime-600 dark:text-lime-400 hover:underline shrink-0" />}
      </span>
    </div>
  )
}

export function CnpjCard({ cnpj, editable = false, onChange, onClose }) {
  const [d, setD] = useState(null)     // resposta original (razao/situacao/abertura)
  const [f, setF] = useState(BLANK)    // campos controlados (edição)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let alive = true
    setD(null); setErr(null); setF(BLANK)
    api.cnpjInfo(cnpj)
      .then((data) => {
        if (!alive) return
        setD(data)
        const fields = {
          nome_fantasia: data.nome_fantasia || '',
          logradouro: data.logradouro || '',
          // número some quando é "S/N": vira input vazio em destaque pra preencher.
          numero: isSN(data.numero) ? '' : (data.numero || ''),
          complemento: data.complemento || '',
          bairro: data.bairro || '', cidade: data.cidade || '',
          uf: data.uf || '', cep: data.cep || '',
          telefone: data.telefone || '', email: data.email || '',
        }
        setF(fields)
        // NÃO sobe onChange aqui: overrides só deve carregar o que o operador
        // EDITAR de fato. Sem isso, o payload do run reaplicaria os valores
        // originais por cima do que o backend já tem (e mascararia o backfill).
      })
      .catch((e) => alive && setErr(e.message))
    return () => { alive = false }
  // onChange é setter estável; não entra nas deps p/ não re-buscar.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cnpj])

  const set = (k, v) => setF((cur) => { const n = { ...cur, [k]: v }; onChange?.(n); return n })

  const ativa = /ativ/i.test(d?.situacao || '')
  const numFalta = editable ? !f.numero.trim() : isSN(d?.numero)

  return (
    <div className="mt-3 rounded-xl border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-950 p-3.5">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="font-mono text-sm font-medium">{cnpj}</span>
        <div className="flex items-center gap-2">
          {d?.situacao && <Badge tone={ativa ? 'green' : 'amber'}>{d.situacao}</Badge>}
          {onClose && <button onClick={onClose} className="text-xs text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">fechar ✕</button>}
        </div>
      </div>

      {err && <p className="text-xs text-red-500 py-2">{err}</p>}
      {!d && !err && <p className="text-xs text-neutral-400 py-2">Consultando BrasilAPI…</p>}

      {d && (
        <>
          {/* Identidade — razão social é sempre só leitura (oficial). */}
          <div className="pb-1">
            <span className="block text-[10px] font-semibold uppercase tracking-wider text-neutral-400 mb-0.5">Razão social</span>
            <span className="text-sm font-medium break-words">{d.razao_social || '(sem razão social)'}</span>
          </div>

          <div className="grid grid-cols-6 gap-x-3 gap-y-2">
            {editable ? (
              <>
                <SectionLabel>Identidade</SectionLabel>
                <EditCell label="Nome fantasia" span={6} value={f.nome_fantasia} onChange={(v) => set('nome_fantasia', v)} placeholder="(opcional)" />

                <SectionLabel>Endereço</SectionLabel>
                <EditCell label="Logradouro" span={4} value={f.logradouro} onChange={(v) => set('logradouro', v)} />
                <EditCell
                  label="Número" span={2} value={f.numero} onChange={(v) => set('numero', v)}
                  warn={numFalta} placeholder="ex.: 608"
                  hint="BrasilAPI não trouxe — preencha"
                />
                <EditCell label="Bairro" span={3} value={f.bairro} onChange={(v) => set('bairro', v)} />
                <EditCell label="Complemento" span={3} value={f.complemento} onChange={(v) => set('complemento', v)} placeholder="(opcional)" />
                <EditCell label="Cidade" span={3} value={f.cidade} onChange={(v) => set('cidade', v)} />
                <EditCell label="UF" span={1} value={f.uf} onChange={(v) => set('uf', v)} />
                <EditCell label="CEP" span={2} value={f.cep} onChange={(v) => set('cep', v)} />

                <SectionLabel>Contato</SectionLabel>
                <EditCell label="Telefone" span={3} value={f.telefone} onChange={(v) => set('telefone', v)} placeholder="(XX) XXXXX-XXXX" />
                <EditCell label="E-mail" span={3} value={f.email} onChange={(v) => set('email', v)} placeholder="(opcional)" />
              </>
            ) : (
              <>
                <SectionLabel>Endereço</SectionLabel>
                <ReadCell label="Logradouro" span={4} value={d.logradouro} />
                <ReadCell label="Número" span={2} value={isSN(d.numero) ? '' : d.numero} warn={numFalta} />
                <ReadCell label="Bairro" span={3} value={d.bairro} />
                <ReadCell label="Cidade / UF" span={3} value={[d.cidade, d.uf].filter(Boolean).join(' / ')} />
                <ReadCell label="CEP" span={3} value={d.cep} copy />

                <SectionLabel>Contato</SectionLabel>
                <ReadCell label="Telefone" span={3} value={d.telefone} copy />
                <ReadCell label="E-mail" span={3} value={d.email} copy />
                <ReadCell label="Nome fantasia" span={6} value={d.nome_fantasia} />
              </>
            )}
          </div>

          {d.abertura && <p className="mt-3 text-[11px] text-neutral-400">Aberta em {d.abertura}</p>}
        </>
      )}
    </div>
  )
}

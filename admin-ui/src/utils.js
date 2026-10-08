// "há Xs / Xm / Xh / Xd" a partir de um ISO string ou epoch ms.
export function timeAgo(ts) {
  if (!ts) return '—'
  const d = typeof ts === 'number' ? ts : Date.parse(ts)
  if (Number.isNaN(d)) return '—'
  const s = Math.max(0, Math.floor((Date.now() - d) / 1000))
  if (s < 60) return `há ${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `há ${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `há ${h}h`
  return `há ${Math.floor(h / 24)}d`
}

// Data/hora local curta p/ colunas de tabela.
export function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(typeof ts === 'number' ? ts : Date.parse(ts))
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function cx(...xs) {
  return xs.filter(Boolean).join(' ')
}

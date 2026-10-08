// Helpers de tempo p/ a lista de runs e o indicador de frescor.

// Aceita unix em segundos (backend) OU milissegundos (Date.now). Normaliza p/ ms.
export function toMs(t) {
  if (!t) return null
  return t < 1e12 ? t * 1000 : t
}

// "agora", "há 12s", "há 4 min", "há 2 h", "há 3 d".
export function timeAgo(t, nowMs = Date.now()) {
  const ms = toMs(t)
  if (!ms) return '—'
  const s = Math.max(0, Math.round((nowMs - ms) / 1000))
  if (s < 5) return 'agora'
  if (s < 60) return `há ${s}s`
  const m = Math.round(s / 60)
  if (m < 60) return `há ${m} min`
  const h = Math.round(m / 60)
  if (h < 24) return `há ${h} h`
  return `há ${Math.round(h / 24)} d`
}

// Duração compacta entre dois instantes: "1m 20s", "45s", "2h 3m".
export function fmtDuration(start, end) {
  const a = toMs(start), b = toMs(end)
  if (!a) return null
  const s = Math.max(0, Math.round(((b || Date.now()) - a) / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60), rs = s % 60
  if (m < 60) return rs ? `${m}m ${rs}s` : `${m}m`
  const h = Math.floor(m / 60), rm = m % 60
  return rm ? `${h}h ${rm}m` : `${h}h`
}

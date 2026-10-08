// Cliente da API admin. Injeta Bearer, trata 401, e — quando VITE_USE_MOCK=true —
// serve dados de exemplo (sem rede). O contrato segue a Frente 3:
//   POST /admin/login {username,password} -> {token}
//   GET  /admin/instances
//   GET  /admin/instances/{id}/runs?status=
//   GET  /admin/instances/{id}/runs/{run_id}/log
//   GET  /admin/instances/{id}/pools
import { mockDb } from './mock.js'

const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'
const BASE = (import.meta.env.VITE_ADMIN_API || '').replace(/\/$/, '')

const TOKEN_KEY = 'admin_token'
export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY))

// Lançado em 401 — a App escuta e devolve o usuário ao login.
export class AuthError extends Error {}

const delay = (ms) => new Promise((r) => setTimeout(r, ms))

async function real(path, { method = 'GET', body } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  let res
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new Error('sem conexão com o coletor')
  }
  if (res.status === 401) {
    setToken(null)
    throw new AuthError('sessão expirada')
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  if (res.status === 204) return null
  return res.json()
}

async function mock(fn) {
  await delay(250 + Math.floor(performance.now() % 200))
  return fn()
}

export const api = {
  async login(username, password) {
    if (USE_MOCK) {
      const { token } = await mock(() => mockDb.login(username, password))
      setToken(token)
      return { token }
    }
    const out = await real('/admin/login', { method: 'POST', body: { username, password } })
    if (out?.token) setToken(out.token)
    return out
  },

  logout() {
    setToken(null)
  },

  listInstances() {
    if (USE_MOCK) return mock(() => mockDb.instances())
    return real('/admin/instances')
  },

  listRuns(instanceId, status) {
    if (USE_MOCK) return mock(() => mockDb.runs(instanceId, status))
    const q = status ? `?status=${encodeURIComponent(status)}` : ''
    return real(`/admin/instances/${encodeURIComponent(instanceId)}/runs${q}`)
  },

  getRunLog(instanceId, runId) {
    if (USE_MOCK) return mock(() => mockDb.runLog(instanceId, runId))
    return real(`/admin/instances/${encodeURIComponent(instanceId)}/runs/${encodeURIComponent(runId)}/log`)
  },

  getPools(instanceId) {
    if (USE_MOCK) return mock(() => mockDb.pools(instanceId))
    return real(`/admin/instances/${encodeURIComponent(instanceId)}/pools`)
  },
}

export const isMock = USE_MOCK

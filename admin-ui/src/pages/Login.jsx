import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { ShieldCheck } from 'lucide-react'
import { useAuth } from '../auth.jsx'
import { isMock } from '../api.js'
import { btnPrimary, inputCls } from '../components/ui.jsx'

export default function Login() {
  const { login } = useAuth()
  const nav = useNavigate()
  const loc = useLocation()
  const [username, setUser] = useState('')
  const [password, setPass] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const dest = loc.state?.from || '/instances'

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await login(username, password)
      nav(dest, { replace: true })
    } catch (e2) {
      setErr(e2.message || 'falha no login')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-full flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2">
          <span className="grid h-9 w-9 place-items-center rounded-lg bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <ShieldCheck size={18} />
          </span>
          <div>
            <h1 className="text-lg font-bold tracking-tight leading-none">Meta Farm · Admin</h1>
            <p className="text-xs text-neutral-500 dark:text-neutral-400">Console multi-instância</p>
          </div>
        </div>

        <form onSubmit={submit} className="rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-5 space-y-3">
          <label className="block">
            <span className="text-xs font-medium text-neutral-500 dark:text-neutral-400">Usuário</span>
            <input className={`mt-1 ${inputCls}`} value={username} onChange={(e) => setUser(e.target.value)} autoFocus autoComplete="username" />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-neutral-500 dark:text-neutral-400">Senha</span>
            <input type="password" className={`mt-1 ${inputCls}`} value={password} onChange={(e) => setPass(e.target.value)} autoComplete="current-password" />
          </label>

          {err && <p className="text-sm text-red-600 dark:text-red-400">{err}</p>}

          <button type="submit" disabled={busy} className={`${btnPrimary} w-full py-2`}>
            {busy ? 'Entrando…' : 'Entrar'}
          </button>

          {isMock && (
            <p className="text-[11px] text-center text-neutral-400">
              Modo mock ativo — qualquer usuário/senha entra.
            </p>
          )}
        </form>
      </div>
    </div>
  )
}

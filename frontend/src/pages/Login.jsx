import { useState } from 'react'
import { LogIn } from 'lucide-react'
import { login } from '../api.js'
import { btnPrimary, inputCls } from '../components/ui.jsx'

// Tela de login do operador. Em sucesso chama `onAuthed()` para o App revelar
// o painel; em erro mostra a mensagem (credencial inválida / servidor mal
// configurado) sem sair da tela.
export default function Login({ onAuthed }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await login(username, password)
      onAuthed()
    } catch (ex) {
      setErr(ex.message || 'Falha no login')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-neutral-50 dark:bg-neutral-950 text-neutral-900 dark:text-neutral-100 p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-6 shadow-sm"
      >
        <div className="mb-6 flex items-center gap-2.5">
          <svg viewBox="0 0 48 48" className="h-9 w-9 rounded-xl" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <defs>
              <linearGradient id="vtxlg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#a3e635" /><stop offset="1" stopColor="#22d3ee" />
              </linearGradient>
            </defs>
            <rect width="48" height="48" rx="12" fill="#0a0a0a" />
            <g fill="none" stroke="url(#vtxlg)" strokeWidth="2.6" strokeLinecap="round">
              <path d="M24 8 C 34 8, 40 14, 40 24" />
              <path d="M40 24 C 40 34, 34 40, 24 40" opacity="0.85" />
              <path d="M24 40 C 14 40, 8 34, 8 24" opacity="0.7" />
              <path d="M8 24 C 8 16, 13 12, 20 12" opacity="0.55" />
            </g>
            <circle cx="24" cy="24" r="3.4" fill="url(#vtxlg)" />
          </svg>
          <div className="leading-tight">
            <span className="block font-bold tracking-tight">Bot-Sr.Vortex</span>
            <span className="block text-[9.5px] font-semibold text-lime-600 dark:text-lime-400 uppercase tracking-[0.18em]">Automation Suite</span>
          </div>
        </div>

        <h1 className="text-lg font-bold tracking-tight">Entrar</h1>
        <p className="mb-5 text-sm text-neutral-500 dark:text-neutral-400">
          Acesso restrito ao operador.
        </p>

        <label className="block text-sm font-medium mb-1.5">Usuário</label>
        <input
          className={`${inputCls} mb-4`}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
          autoComplete="username"
        />

        <label className="block text-sm font-medium mb-1.5">Senha</label>
        <input
          type="password"
          className={`${inputCls} mb-4`}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />

        {err && (
          <p className="mb-4 rounded-lg bg-red-100 dark:bg-red-950/50 px-3 py-2 text-sm text-red-700 dark:text-red-300">
            {err}
          </p>
        )}

        <button type="submit" disabled={busy} className={`${btnPrimary} flex w-full items-center justify-center gap-2 px-4 py-2.5`}>
          <LogIn size={16} />
          {busy ? 'Entrando…' : 'Entrar'}
        </button>
      </form>
    </div>
  )
}

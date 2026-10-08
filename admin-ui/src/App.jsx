import { useState } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { LogOut, Moon, Sun, ShieldCheck } from 'lucide-react'
import { useAuth } from './auth.jsx'
import { isMock } from './api.js'
import Login from './pages/Login.jsx'
import Overview from './pages/Overview.jsx'
import Instance from './pages/Instance.jsx'

export default function App() {
  const { isAuthed } = useAuth()
  return (
    <Routes>
      <Route path="/login" element={isAuthed ? <Navigate to="/instances" replace /> : <Login />} />
      <Route element={<Protected />}>
        <Route path="/instances" element={<Overview />} />
        <Route path="/instances/:id" element={<Instance />} />
      </Route>
      <Route path="*" element={<Navigate to={isAuthed ? '/instances' : '/login'} replace />} />
    </Routes>
  )
}

// Layout protegido: sem token -> volta pro login guardando o destino.
function Protected() {
  const { isAuthed } = useAuth()
  const loc = useLocation()
  if (!isAuthed) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  return (
    <div className="min-h-full flex flex-col">
      <TopBar />
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
        {/* As rotas filhas renderizam aqui via Outlet */}
        <Outlet />
      </main>
    </div>
  )
}

function TopBar() {
  const { logout } = useAuth()
  const nav = useNavigate()
  const [dark, setDark] = useState(document.documentElement.classList.contains('dark'))

  const toggleTheme = () => {
    const next = !dark
    setDark(next)
    document.documentElement.classList.toggle('dark', next)
    localStorage.setItem('admin_theme', next ? 'dark' : 'light')
  }

  return (
    <header className="sticky top-0 z-10 border-b border-neutral-200 dark:border-neutral-800 bg-white/80 dark:bg-neutral-950/80 backdrop-blur">
      <div className="mx-auto flex w-full max-w-6xl items-center justify-between px-4 py-3">
        <button onClick={() => nav('/instances')} className="flex items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <ShieldCheck size={15} />
          </span>
          <span className="font-bold tracking-tight">Meta Farm · Admin</span>
          {isMock && <span className="rounded-full bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300 px-2 py-0.5 text-[10px] font-semibold">MOCK</span>}
        </button>
        <div className="flex items-center gap-1">
          <button onClick={toggleTheme} aria-label="Alternar tema" className="grid h-8 w-8 place-items-center rounded-lg text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition">
            {dark ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button onClick={() => { logout(); nav('/login') }} className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition">
            <LogOut size={15} /> Sair
          </button>
        </div>
      </div>
    </header>
  )
}

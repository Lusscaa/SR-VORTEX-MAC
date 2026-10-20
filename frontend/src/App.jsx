import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, Building2, Rocket, History,
  Moon, Sun, Settings as SettingsIcon, LogOut, AlertCircle, Globe, KeyRound,
  Send, UserPlus,
} from 'lucide-react'

// Emblema próprio do Bot-Sr.Vortex — espiral/vórtice (SVG inline, sem plágio).
function VortexLogo({ className = '' }) {
  return (
    <svg viewBox="0 0 48 48" className={className} xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="vtxg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#a3e635" />
          <stop offset="1" stopColor="#22d3ee" />
        </linearGradient>
      </defs>
      <rect width="48" height="48" rx="12" fill="#0a0a0a" />
      <g fill="none" stroke="url(#vtxg)" strokeWidth="2.6" strokeLinecap="round">
        <path d="M24 8 C 34 8, 40 14, 40 24" />
        <path d="M40 24 C 40 34, 34 40, 24 40" opacity="0.85" />
        <path d="M24 40 C 14 40, 8 34, 8 24" opacity="0.7" />
        <path d="M8 24 C 8 16, 13 12, 20 12" opacity="0.55" />
        <path d="M20 12 C 27 12, 31 16, 31 22" opacity="0.5" />
      </g>
      <circle cx="24" cy="24" r="3.4" fill="url(#vtxg)" />
    </svg>
  )
}
// Mascote ONI samurai-mecha (機械鬼) — SVG inline no estilo da referência:
// chifres largos, olhos ciano furiosos, máscara de demônio com presas, cabos e
// acentos vermelhos. Sem plágio, sem asset externo. Olhos piscam.
function VortexMascot({ className = '' }) {
  return (
    <svg viewBox="0 0 100 100" className={className} xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="mk-gold" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f8dd93" /><stop offset="1" stopColor="#c8891f" />
        </linearGradient>
        <linearGradient id="mk-metal" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#e2e8f0" /><stop offset="0.5" stopColor="#94a3b8" /><stop offset="1" stopColor="#475569" />
        </linearGradient>
        <linearGradient id="mk-face" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4a1522" /><stop offset="1" stopColor="#160408" />
        </linearGradient>
        <radialGradient id="mk-eye" cx="0.5" cy="0.5" r="0.6">
          <stop offset="0" stopColor="#eaffff" /><stop offset="0.5" stopColor="#22d3ee" /><stop offset="1" stopColor="#0e7490" />
        </radialGradient>
      </defs>

      {/* chifres (kuwagata) */}
      <path d="M40 30 Q12 24 12 4 Q4 20 24 30 Q30 30 40 34 Z" fill="url(#mk-gold)" stroke="#7a5510" strokeWidth="1" />
      <path d="M60 30 Q88 24 88 4 Q96 20 76 30 Q70 30 60 34 Z" fill="url(#mk-gold)" stroke="#7a5510" strokeWidth="1" />
      {/* protetores laterais */}
      <path d="M18 46 Q10 50 12 62 L20 60 Q18 52 24 48 Z" fill="url(#mk-gold)" stroke="#7a5510" strokeWidth="0.8" />
      <path d="M82 46 Q90 50 88 62 L80 60 Q82 52 76 48 Z" fill="url(#mk-gold)" stroke="#7a5510" strokeWidth="0.8" />

      {/* domo do capacete */}
      <path d="M26 46 Q26 18 50 15 Q74 18 74 46 Q50 34 26 46 Z" fill="url(#mk-metal)" stroke="#334155" strokeWidth="1" />
      {/* disco da testa com X */}
      <circle cx="50" cy="33" r="7" fill="url(#mk-gold)" stroke="#7a5510" strokeWidth="1" />
      <path d="M46.5 29.5 L53.5 36.5 M53.5 29.5 L46.5 36.5" stroke="#22d3ee" strokeWidth="1.6" strokeLinecap="round" />

      {/* placa do rosto */}
      <path d="M28 44 Q50 40 72 44 L68 66 Q60 80 50 84 Q40 80 32 66 Z" fill="url(#mk-face)" stroke="#22d3ee" strokeWidth="1.2" />

      {/* sobrancelhas furiosas */}
      <path d="M32 50 L46 55 L44 51 Z" fill="#0b0305" />
      <path d="M68 50 L54 55 L56 51 Z" fill="#0b0305" />
      {/* olhos ciano (slant furioso), piscando */}
      <path d="M34 54 L45 51 L44 57 L36 59 Z" fill="url(#mk-eye)">
        <animate attributeName="opacity" values="1;0.5;1" dur="2s" repeatCount="indefinite" />
      </path>
      <path d="M66 54 L55 51 L56 57 L64 59 Z" fill="url(#mk-eye)">
        <animate attributeName="opacity" values="1;0.5;1" dur="2s" begin="0.25s" repeatCount="indefinite" />
      </path>

      {/* vinco do nariz */}
      <path d="M50 58 L47 66 L53 66 Z" fill="#2a0a12" />

      {/* boca rosnando */}
      <path d="M35 68 Q50 64 65 68 Q58 80 50 82 Q42 80 35 68 Z" fill="#120306" stroke="#7f1d3a" strokeWidth="0.8" />
      {/* presas de cima */}
      <g fill="#f1f5f9">
        <path d="M38 68 L41 74 L44 68 Z" /><path d="M45 68.5 L48 75 L51 68.5 Z" />
        <path d="M52 68.5 L55 75 L58 68.5 Z" /><path d="M59 68 L62 73 L64 68 Z" />
      </g>
      {/* presas de baixo */}
      <g fill="#e2e8f0">
        <path d="M43 79 L45 74 L47 79 Z" /><path d="M53 79 L55 74 L57 79 Z" />
      </g>

      {/* cabos nas bochechas */}
      <g stroke="#64748b" strokeWidth="2.4" strokeLinecap="round" fill="none">
        <path d="M30 60 Q24 64 26 72" /><path d="M70 60 Q76 64 74 72" />
      </g>
      <circle cx="26" cy="72" r="2.2" fill="#fb7185" /><circle cx="74" cy="72" r="2.2" fill="#fb7185" />
      {/* riscos vermelhos */}
      <path d="M22 40 l-6 -3 M78 40 l6 -3" stroke="#fb2c56" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}
import Dashboard from './pages/Dashboard.jsx'
import Disparar from './pages/Disparar.jsx'
import BMs from './pages/BMs.jsx'
import Historico from './pages/Historico.jsx'
import Settings from './pages/Settings.jsx'
import Apis from './pages/Apis.jsx'
import GerarSite from './pages/GerarSite.jsx'
import SoftBlockedAccounts from './pages/SoftBlockedAccounts.jsx'
import PendingDiscard from './pages/PendingDiscard.jsx'
import FbCreator from './pages/FbCreator.jsx'
import Login from './pages/Login.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import { auth, logout } from './api.js'

const NAV = [
  { to: '/', label: 'Início', icon: LayoutDashboard, end: true },
  { to: '/disparar', label: 'Disparar', icon: Rocket },
  { to: '/bms', label: 'Business Managers', icon: Building2 },
  { to: '/settings', label: 'Credenciais', icon: SettingsIcon },
  { to: '/apis', label: 'APIs', icon: KeyRound },
  { to: '/gerar-site', label: 'Gerar Site / PDFs', icon: Globe },
  { to: '/historico', label: 'Histórico', icon: History },
  { to: '/pending-discard', label: 'Descartes', icon: AlertCircle },
  { to: '/fb-creator', label: 'FB-Creator', icon: UserPlus },
]

function useDarkMode() {
  const [dark, setDark] = useState(() => localStorage.getItem('theme') === 'dark')
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])
  return [dark, setDark]
}

function Sidebar({ dark, setDark }) {
  return (
    <aside className="w-56 shrink-0 bg-white dark:bg-[#06130d]/70 dark:backdrop-blur-xl border-r border-neutral-200 dark:border-emerald-400/15 flex flex-col relative overflow-hidden">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <VortexLogo className="h-9 w-9 shrink-0 rounded-xl shadow-sm" />
        <div className="leading-tight">
          <span className="font-display block font-extrabold text-neutral-900 cyber-gold tracking-wide">BOT-SR.VORTEX</span>
          <span className="block text-[9px] font-bold text-lime-600 dark:text-emerald-300/70 uppercase tracking-[0.28em]">Automation Suite</span>
        </div>
      </div>
      {/* Kanji watermark (機械鬼 → aqui: 自動化 "automação"). */}
      <div className="hud-kanji red hidden dark:block absolute top-24 right-1 text-4xl leading-none opacity-80 z-0" aria-hidden="true">自動化</div>
      {/* Decoração HUD: hazard + boot-log de terminal + status */}
      <div className="hidden dark:block px-4 pb-3 relative z-10">
        <div className="hud-hazard mb-1.5" />
        <div className="flex items-center justify-between hud-tag mb-2">
          <span>◤ SYS.ONLINE</span><span>v2.4.8</span>
        </div>
        <div className="hud-code border-l border-pink-400/30 pl-2 mb-2">
          {'> '}<span className="g">boot</span>{' vortex.core\n'}
          {'> '}<span className="k">01001100</span>{' 01011111\n'}
          {'> '}<span className="r">#include</span>{' <shutdown>\n'}
          {'root@vortex'}<span className="g">:~$</span>{' ready ▓'}
        </div>
        <div className="flex items-center justify-between">
          <span className="hud-tag text-rose-400/80" style={{ textShadow: '0 0 8px rgba(244,63,94,.5)' }}>ERR·404·HUMAN</span>
          <span className="hud-wave flex">{Array.from({ length: 7 }).map((_, i) => <i key={i} style={{ animationDelay: `${i * 0.09}s` }} />)}</span>
        </div>
      </div>
      <nav className="flex-1 px-2.5 space-y-1">
        <p className="px-2.5 pb-1.5 pt-2 text-[10px] font-semibold uppercase tracking-wider text-neutral-400 dark:text-emerald-300/50">▸ Menu</p>
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to} to={to} end={end}
            className={({ isActive }) =>
              `nav-cyber flex items-center gap-3 rounded-md px-3 py-2.5 text-[15px] font-bold uppercase tracking-wide transition-all duration-150 hover:translate-x-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60 ${
                isActive
                  ? 'active-nav bg-neutral-900 text-white dark:bg-gradient-to-r dark:from-amber-300/90 dark:to-emerald-400/90 dark:text-[#06130d] dark:font-black dark:shadow-[0_0_20px_-4px_rgba(52,211,153,0.65)]'
                  : 'text-neutral-600 dark:text-emerald-100/80 hover:bg-neutral-100 dark:hover:bg-emerald-400/10'
              }`}
          >
            <Icon size={19} className="shrink-0" /> {label}
          </NavLink>
        ))}
      </nav>
      <div className="cred-card mx-2.5 mb-2 relative overflow-hidden rounded-xl border border-amber-300/40 dark:border-emerald-400/25 bg-gradient-to-br from-neutral-50 to-neutral-100 dark:from-[#0a1c13] dark:to-[#06130d] px-3 py-3 text-center">
        {/* Faixa hazard + mascote cartoon cyberpunk */}
        <div className="hud-hazard mb-2 hidden dark:block" />
        <VortexMascot className="mx-auto mb-1 h-20 w-20 drop-shadow-[0_0_14px_rgba(34,211,238,0.5)]" />
        <p className="font-display text-[9px] font-black uppercase tracking-[0.32em] text-neutral-500 dark:text-emerald-300/70 relative z-10">Created by</p>
        <p className="font-display text-xl font-black leading-none my-1 cyber-gold relative z-10">LUSCA</p>
        <a
          href="https://t.me/SrVortex"
          target="_blank"
          rel="noreferrer"
          className="relative z-10 mt-1 inline-flex items-center gap-1.5 rounded-md border border-sky-300/40 dark:border-sky-400/40 bg-sky-50/60 dark:bg-sky-500/10 px-2.5 py-1 text-[11px] font-bold text-sky-700 dark:text-sky-300 hover:bg-sky-100 dark:hover:bg-sky-500/20 transition"
        >
          <Send size={12} /> Telegram @SrVortex
        </a>
      </div>
      <button
        type="button"
        onClick={() => setDark(!dark)}
        aria-label={dark ? 'Ativar modo claro' : 'Ativar modo escuro'}
        className="mx-2.5 mt-2.5 flex items-center justify-center gap-2 rounded-lg border border-neutral-200 dark:border-neutral-700 px-3 py-2 text-sm font-medium text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60"
      >
        {dark ? <Sun size={15} /> : <Moon size={15} />}
        {dark ? 'Modo claro' : 'Modo escuro'}
      </button>
      <button
        type="button"
        onClick={logout}
        aria-label="Sair"
        className="m-2.5 flex items-center justify-center gap-2 rounded-lg border border-neutral-200 dark:border-neutral-700 px-3 py-2 text-sm font-medium text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-500/60"
      >
        <LogOut size={15} /> Sair
      </button>
    </aside>
  )
}

// Camada de fundo cyberpunk (sticker-sheet HUD): target concêntrico, traços de
// circuito, barcode, AREA 07, cruzes, setas, hachura — espalhados nas bordas.
// Puramente decorativo, só no dark, atrás do conteúdo (z-0, pointer-events none).
function HudBackdrop() {
  const G = '#e8c14a', E = '#34d399', C = '#7dd3fc', M = '#ec4899'
  return (
    <div className="hidden dark:block fixed inset-0 z-0 pointer-events-none overflow-hidden" aria-hidden="true">
      {/* Alvo concêntrico (target) — direita/topo */}
      <svg className="absolute top-[13%] right-[2%] w-44 h-44 opacity-[0.16]" viewBox="0 0 120 120">
        <g fill="none" strokeWidth="1.6">
          <circle cx="60" cy="60" r="54" stroke={G} />
          <circle cx="60" cy="60" r="41" stroke={E} strokeDasharray="4 4" />
          <circle cx="60" cy="60" r="27" stroke={G} />
        </g>
        <g stroke={C} strokeWidth="1.6">
          <line x1="60" y1="0" x2="60" y2="16" /><line x1="60" y1="104" x2="60" y2="120" />
          <line x1="0" y1="60" x2="16" y2="60" /><line x1="104" y1="60" x2="120" y2="60" />
        </g>
        <circle cx="60" cy="60" r="4" fill={M} />
      </svg>

      {/* Traços de circuito (PCB) — borda esquerda */}
      <svg className="absolute top-[26%] left-0 w-24 h-96 opacity-[0.14]" viewBox="0 0 90 360" fill="none" stroke={E} strokeWidth="2">
        <path d="M45 0 V50 M45 50 H14 M14 50 V150 M45 50 H76 M76 50 V110 M45 150 V360 M45 210 H16 M16 210 V300 M45 260 H74 M74 260 V330" />
        <g fill={G} stroke="none">
          <circle cx="14" cy="50" r="3.5" /><circle cx="76" cy="110" r="3.5" /><circle cx="16" cy="300" r="3.5" /><circle cx="74" cy="330" r="3.5" /><circle cx="45" cy="0" r="3.5" />
        </g>
      </svg>

      {/* Barcode vertical — borda direita/baixo */}
      <svg className="absolute bottom-[20%] right-[1.5%] w-10 h-40 opacity-[0.18]" viewBox="0 0 40 160" fill={G}>
        {[0,6,10,18,22,26,34].map((y,i)=>(<rect key={i} x="0" y={y*4} width="40" height={(i%3)+2} />))}
      </svg>

      {/* AREA 07 (tag) — canto inferior esquerdo */}
      <div className="absolute bottom-[11%] left-[2.5%] opacity-[0.18]">
        <div className="font-display text-[10px] font-black tracking-[0.3em] px-2 py-1 border" style={{ color: G, borderColor: G }}>AREA 07</div>
        <div className="hud-hazard mt-1 w-24" />
      </div>

      {/* Seta grande pra baixo — inferior direito */}
      <svg className="absolute bottom-[9%] right-[9%] w-16 h-28 opacity-[0.14]" viewBox="0 0 60 110" fill="none" stroke={M} strokeWidth="3">
        <line x1="30" y1="0" x2="30" y2="78" />
        <path d="M8 60 L30 92 L52 60" />
      </svg>

      {/* Cluster de cruzes ┼ — topo esquerdo */}
      <svg className="absolute top-[9%] left-[7%] w-28 h-16 opacity-[0.16]" viewBox="0 0 140 60" stroke={C} strokeWidth="1.6">
        {[0,1,2,3].map(c=>[0,1].map(r=>(
          <g key={`${c}-${r}`} transform={`translate(${c*34+10} ${r*30+12})`}>
            <line x1="-7" y1="0" x2="7" y2="0" /><line x1="0" y1="-7" x2="0" y2="7" />
          </g>
        )))}
      </svg>

      {/* Chevrons »» — meio direita */}
      <svg className="absolute top-[46%] right-[6%] w-10 h-24 opacity-[0.16]" viewBox="0 0 40 120" fill="none" stroke={E} strokeWidth="3">
        {[0,26,52].map((y,i)=>(<path key={i} d={`M6 ${y+6} L24 ${y+22} L6 ${y+38}`} />))}
      </svg>

      {/* Quadrado hachurado (diagonal) — meio esquerda */}
      <svg className="absolute bottom-[33%] left-[3.5%] w-20 h-20 opacity-[0.13]" viewBox="0 0 80 80">
        <rect x="2" y="2" width="76" height="76" fill="none" stroke={G} strokeWidth="1.5" />
        <g stroke={M} strokeWidth="2">
          {[0,12,24,36,48,60].map((o,i)=>(<line key={i} x1={o} y1="80" x2="80" y2={o} />))}
        </g>
      </svg>

      {/* X (crosshair) grande — topo centro-direita */}
      <svg className="absolute top-[4%] right-[26%] w-14 h-14 opacity-[0.13]" viewBox="0 0 60 60" stroke={M} strokeWidth="3" fill="none">
        <rect x="3" y="3" width="54" height="54" stroke={G} strokeWidth="1.5" />
        <line x1="14" y1="14" x2="46" y2="46" /><line x1="46" y1="14" x2="14" y2="46" />
      </svg>

      {/* Meia-lua de pontos (halftone) — meio inferior */}
      <svg className="absolute bottom-[6%] left-[38%] w-40 h-10 opacity-[0.12]" viewBox="0 0 200 40" fill={E}>
        {Array.from({length:11}).map((_,i)=>(<circle key={i} cx={10+i*18} cy="20" r={5-Math.abs(i-5)*0.6} />))}
      </svg>
    </div>
  )
}

export default function App() {
  const [dark, setDark] = useDarkMode()
  const location = useLocation()
  const [authed, setAuthed] = useState(() => auth.isAuthed())

  // Logout (botão) e 401 (token expirado/inválido) disparam o mesmo evento
  // global — voltamos à tela de login sem recarregar a página.
  useEffect(() => {
    const onUnauth = () => setAuthed(false)
    window.addEventListener('panel-unauthorized', onUnauth)
    return () => window.removeEventListener('panel-unauthorized', onUnauth)
  }, [])

  if (!authed) return <Login onAuthed={() => setAuthed(true)} />

  return (
    <div className="flex h-full bg-neutral-50 dark:bg-transparent text-neutral-900 dark:text-neutral-100">
      <Sidebar dark={dark} setDark={setDark} />
      <main
        className="flex-1 overflow-y-auto relative"
        style={{
          background: dark ? 'transparent' : 'linear-gradient(135deg, #f8f8f8 0%, #f0f0f0 50%, #e8e8e8 100%)',
        }}
      >
        <HudBackdrop />
        {/* Acento cyber (dourado→verde) no topo */}
        <div
          className="pointer-events-none fixed top-0 left-0 right-0 h-[2px] z-[51]"
          style={{ background: 'linear-gradient(90deg, transparent 0%, rgba(236,72,153,0.6) 14%, rgba(232,193,74,0.65) 34%, rgba(52,211,153,0.8) 58%, rgba(125,211,252,0.6) 80%, transparent 100%)' }}
        />

        <div className="mx-auto max-w-[1760px] p-6 xl:p-8 relative z-10">
          <ErrorBoundary routeKey={location.pathname}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/disparar" element={<Disparar />} />
              <Route path="/historico" element={<Historico />} />
              <Route path="/pending-discard" element={<PendingDiscard />} />
              <Route path="/bms" element={<BMs />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/apis" element={<Apis />} />
              <Route path="/gerar-site" element={<GerarSite />} />
              <Route path="/fb-creator" element={<FbCreator />} />
              {/* rotas antigas → novos destinos (links/bookmarks não quebram) */}
              <Route path="/lote" element={<Navigate to="/disparar" replace />} />
              <Route path="/manual" element={<Navigate to="/disparar" replace />} />
              <Route path="/runs" element={<Navigate to="/" replace />} />
              <Route path="/logs" element={<Navigate to="/" replace />} />
            </Routes>
          </ErrorBoundary>
        </div>
      </main>
    </div>
  )
}

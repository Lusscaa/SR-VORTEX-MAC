import { createContext, useCallback, useContext, useState } from 'react'
import { api, getToken } from './api.js'

const AuthCtx = createContext(null)

export function AuthProvider({ children }) {
  const [token, setTok] = useState(() => getToken())

  const login = async (username, password) => {
    const out = await api.login(username, password)
    setTok(out.token)
    return out
  }

  const logout = () => {
    api.logout()
    setTok(null)
  }

  // Chamado quando qualquer request devolve 401 (AuthError).
  const onUnauthorized = () => setTok(null)

  return (
    <AuthCtx.Provider value={{ token, isAuthed: !!token, login, logout, onUnauthorized }}>
      {children}
    </AuthCtx.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthCtx)
  if (!ctx) throw new Error('useAuth fora do AuthProvider')
  return ctx
}

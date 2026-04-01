import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { apiFetch } from '@/lib/utils'
import { clearAuthToken, getAuthToken, setAuthToken, type AuthUser } from '@/lib/auth'

interface AuthContextValue {
  loading: boolean
  bootstrapped: boolean
  token: string
  user: AuthUser | null
  login: (username: string, password: string) => Promise<void>
  bootstrap: (username: string, password: string) => Promise<void>
  logout: () => void
  refreshMe: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

async function fetchBootstrapStatus() {
  const response = await fetch('/api/auth/bootstrap/status')
  if (!response.ok) {
    throw new Error(await response.text())
  }
  const data = await response.json()
  return Boolean(data?.bootstrapped)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true)
  const [bootstrapped, setBootstrapped] = useState(true)
  const [token, setToken] = useState(() => getAuthToken())
  const [user, setUser] = useState<AuthUser | null>(null)

  const refreshMe = async () => {
    const data = await apiFetch('/auth/me')
    setUser((data?.user || null) as AuthUser | null)
  }

  useEffect(() => {
    let cancelled = false

    const initialize = async () => {
      setLoading(true)
      try {
        const ready = await fetchBootstrapStatus()
        if (cancelled) return
        setBootstrapped(ready)

        if (!ready) {
          clearAuthToken()
          setToken('')
          setUser(null)
          return
        }

        const currentToken = getAuthToken()
        if (!currentToken) {
          setToken('')
          setUser(null)
          return
        }

        setToken(currentToken)
        await refreshMe()
      } catch {
        clearAuthToken()
        setToken('')
        setUser(null)
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    initialize()
    return () => {
      cancelled = true
    }
  }, [])

  const login = async (username: string, password: string) => {
    const data = await apiFetch('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    const nextToken = String(data?.token || '')
    setAuthToken(nextToken)
    setToken(nextToken)
    setBootstrapped(true)
    setUser((data?.user || null) as AuthUser | null)
  }

  const bootstrap = async (username: string, password: string) => {
    const data = await apiFetch('/auth/bootstrap', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    const nextToken = String(data?.token || '')
    setAuthToken(nextToken)
    setToken(nextToken)
    setBootstrapped(true)
    setUser((data?.user || null) as AuthUser | null)
  }

  const logout = () => {
    clearAuthToken()
    setToken('')
    setUser(null)
  }

  const value = useMemo<AuthContextValue>(
    () => ({
      loading,
      bootstrapped,
      token,
      user,
      login,
      bootstrap,
      logout,
      refreshMe,
    }),
    [loading, bootstrapped, token, user],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return context
}

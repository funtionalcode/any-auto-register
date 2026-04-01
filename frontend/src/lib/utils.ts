import { clearAuthToken, getAuthToken, setAuthToken } from './auth'

export const API = '/api'
export const API_BASE = '/api'

export function getToken(): string {
  return getAuthToken()
}

export function setToken(token: string): void {
  setAuthToken(token)
}

export function clearToken(): void {
  clearAuthToken()
}

export async function apiFetch(path: string, opts?: RequestInit) {
  const headers = new Headers(opts?.headers || {})
  const token = getAuthToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  if (!(opts?.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const res = await fetch(API + path, {
    ...opts,
    headers,
  })
  const contentType = String(res.headers.get('content-type') || '')

  if (!res.ok) {
    let errorText = ''
    if (contentType.includes('application/json')) {
      try {
        const data = await res.json()
        errorText = String(data?.detail || data?.message || JSON.stringify(data) || '')
      } catch {
        errorText = ''
      }
    }
    if (!errorText) {
      errorText = await res.text()
    }
    if (res.status === 401 && token) {
      clearAuthToken()
    }
    throw new Error(errorText || `请求失败: HTTP ${res.status}`)
  }

  if (res.status === 204) {
    return null
  }
  if (contentType.includes('application/json')) {
    return res.json()
  }
  return res.text()
}

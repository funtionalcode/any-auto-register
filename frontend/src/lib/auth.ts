export interface AuthUser {
  id: number
  username: string
  role: 'admin' | 'user'
  is_active: boolean
}

export const AUTH_TOKEN_STORAGE_KEY = 'aar.auth.token'

export function getAuthToken() {
  return localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || ''
}

export function setAuthToken(token: string) {
  localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token)
}

export function clearAuthToken() {
  localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY)
}

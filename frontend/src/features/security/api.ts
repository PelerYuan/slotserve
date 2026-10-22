import { request } from '@/api/client'
import type { User } from '@/api/types'

export interface SetupState {
  /** No admin account exists yet: signing up needs the setup code (from the server log or `slotserve setup-code`). */
  setup_required: boolean
}

export interface SudoState {
  active: boolean
  expires_at: string | null
  window_seconds: number
}

export const securityApi = {
  setupState: () => request<SetupState>('GET', '/api/auth/setup'),
  /** Sign up; while no admin exists, `setup_code` is needed instead of the invite code. */
  register: (username: string, password: string, codes: { invite_code?: string; setup_code?: string }) =>
    request<User>('POST', '/api/auth/register', { username, password, ...codes }),
  /** Sudo mode: dangerous actions answer 403 `sudo_required` until the password was entered again. */
  sudoState: () => request<SudoState>('GET', '/api/auth/sudo'),
  enterSudo: (password: string) => request<SudoState>('POST', '/api/auth/sudo', { password }),
}

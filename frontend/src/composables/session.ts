import { reactive } from 'vue'
import { api } from '@/api/client'
import type { AppConfig, User } from '@/api/types'

export const session = reactive<{ user: User | null; config: AppConfig | null; checked: boolean }>({
  user: null,
  config: null,
  checked: false,
})

export async function loadSession(): Promise<User | null> {
  if (!session.config) session.config = await api.config().catch(() => null)
  try {
    session.user = await api.me()
  } catch {
    session.user = null
  }
  session.checked = true
  return session.user
}

export async function logout() {
  await api.logout().catch(() => undefined)
  session.user = null
}

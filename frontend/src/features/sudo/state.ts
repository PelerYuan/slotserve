import { reactive } from 'vue'

/** The password prompt of sudo mode (security/sudo.py): one at a time, shared by every request that needs it. */
export const sudoPrompt = reactive({ open: false })

let pending: Promise<boolean> | null = null
let resolve: ((ok: boolean) => void) | null = null

/** Opens the prompt (or joins the one already open); true once the password was accepted, false when cancelled. */
export function askForSudo(): Promise<boolean> {
  if (!pending) {
    pending = new Promise<boolean>((r) => {
      resolve = r
      sudoPrompt.open = true
    }).finally(() => {
      pending = null
    })
  }
  return pending
}

export function finishSudo(ok: boolean) {
  sudoPrompt.open = false
  const r = resolve
  resolve = null
  r?.(ok)
}

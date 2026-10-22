import { useI18n } from 'vue-i18n'
import { useDialog } from 'naive-ui'
import { ApiError, type ArgsGateFlags } from '@/api/client'

// Launch-argument policy (backend runtime/engines.py): admins confirm flags that can run code or write files on
// the server; members may send arguments outside the allowlist as a request for an admin's approval.
export function useArgsGate() {
  const { t } = useI18n()
  const dialog = useDialog()

  function ask(title: string, content: string, positiveText: string): Promise<boolean> {
    return new Promise((resolve) => {
      dialog.warning({
        title,
        content,
        positiveText,
        negativeText: t('common.cancel'),
        onPositiveClick: () => resolve(true),
        onNegativeClick: () => resolve(false),
        onClose: () => resolve(false),
        onMaskClick: () => resolve(false),
      })
    })
  }

  const flags = (v: unknown) => (Array.isArray(v) ? v.join(' ') : '')

  /** Runs ``send``; when the arguments need a confirmation or an approval, asks and sends again. ``null`` = cancelled. */
  async function withArgsGate<T>(send: (extra: ArgsGateFlags) => Promise<T>): Promise<T | null> {
    try {
      return await send({})
    } catch (e) {
      if (!(e instanceof ApiError)) throw e
      if (e.code === 'args_confirm_required') {
        const ok = await ask(t('argsGate.confirmTitle'), t('argsGate.confirmBody', { flags: flags(e.params.denied) }), t('argsGate.confirm'))
        return ok ? await send({ confirm_args: true }) : null
      }
      if (e.code === 'args_need_approval' && e.params.can_request) {
        const ok = await ask(t('argsGate.requestTitle'), t('argsGate.requestBody', { flags: flags(e.params.needs_approval) }), t('argsGate.request'))
        return ok ? await send({ request_approval: true }) : null
      }
      if (e.code === 'args_need_approval') throw new ApiError(e.status, 'args_need_approval', { flags: flags(e.params.needs_approval) })
      throw e
    }
  }

  return { withArgsGate }
}

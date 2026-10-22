// Opening an environment's web interface through POST /api/devenv/sessions/<id>/web-open: a one-time link in a
// new tab; an admin opening someone else's environment confirms first (that is audited, R1-03).
import { useI18n } from 'vue-i18n'
import { useDialog } from 'naive-ui'
import { ApiError } from '@/api/client'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { devenvApi } from './api'

export function useWebOpen() {
  const { t } = useI18n()
  const dialog = useDialog()
  const { showError } = useErrorMessage()

  // The tab is opened right away (inside the click), so the browser does not block it as a pop-up.
  function blankTab(): Window | null {
    const w = window.open('about:blank', '_blank')
    if (w) w.opener = null
    return w
  }

  async function go(sessionId: number, port: number, confirmAsAdmin: boolean, tab: Window | null) {
    try {
      const out = await devenvApi.webOpen(sessionId, port, confirmAsAdmin)
      if (tab) tab.location.href = out.url
      else window.open(out.url, '_blank', 'noopener')
    } catch (e) {
      tab?.close()
      if (e instanceof ApiError && e.code === 'devenv_open_as_admin_confirm') {
        dialog.warning({
          title: t('devenvUi.web.adminTitle'),
          content: t('errors.devenv_open_as_admin_confirm', { owner: (e.params.owner as string) ?? '?' }),
          positiveText: t('devenvUi.web.adminConfirm'),
          negativeText: t('common.cancel'),
          onPositiveClick: () => {
            void go(sessionId, port, true, blankTab())
          },
        })
        return
      }
      showError(e)
    }
  }

  return {
    open: (sessionId: number, port: number) => go(sessionId, port, false, blankTab()),
  }
}

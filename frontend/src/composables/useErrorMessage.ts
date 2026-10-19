import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { ApiError } from '@/api/client'

export function useErrorMessage() {
  const { t, te } = useI18n()
  const message = useMessage()

  function describe(err: unknown): string {
    if (err instanceof ApiError) {
      const key = `errors.${err.code}`
      return te(key) ? t(key, err.params as Record<string, unknown>) : t('errors.unknown_error')
    }
    return t('errors.unknown_error')
  }

  function showError(err: unknown) {
    message.error(describe(err))
  }

  return { describe, showError, message }
}

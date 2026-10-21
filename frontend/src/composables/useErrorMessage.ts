import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { ApiError } from '@/api/client'
import { formatDateTime } from '@/utils/format'

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/

// Times in error parameters (e.g. a booking's start) are shown in the viewer's time zone.
function readable(params: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).map(([k, v]) => [k, typeof v === 'string' && ISO.test(v) ? formatDateTime(v) : v]),
  )
}

export function useErrorMessage() {
  const { t, te } = useI18n()
  const message = useMessage()

  function describe(err: unknown): string {
    if (err instanceof ApiError) {
      const key = `errors.${err.code}`
      return te(key) ? t(key, readable(err.params as Record<string, unknown>)) : t('errors.unknown_error')
    }
    return t('errors.unknown_error')
  }

  function showError(err: unknown) {
    message.error(describe(err))
  }

  return { describe, showError, message }
}

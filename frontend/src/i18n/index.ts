import { createI18n } from 'vue-i18n'
import en from './en'
import zhCN from './zh-CN'

export type Locale = 'zh-CN' | 'en'
const STORAGE_KEY = 'slotserve.locale'

function initialLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'zh-CN' || saved === 'en') return saved
  } catch {
    /* storage unavailable */
  }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en'
}

export const i18n = createI18n({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: 'en',
  messages: { en, 'zh-CN': zhCN },
})

export function setLocale(locale: Locale) {
  i18n.global.locale.value = locale
  document.documentElement.lang = locale
  try {
    localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    /* ignore */
  }
}

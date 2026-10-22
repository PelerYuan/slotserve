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

// Texts of features/<name>/i18n.ts (`export const en = {...}; export const zh: typeof en = {...}`) are merged
// into the shared ones, so a new feature adds its own file instead of editing en.ts / zh-CN.ts.
// See src/features/README.md.
type Tree = { [key: string]: unknown }
interface FeatureTexts {
  en?: Tree
  zh?: Tree
}

const featureTexts = import.meta.glob<FeatureTexts>('../features/*/i18n.ts', { eager: true })

function isTree(value: unknown): value is Tree {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function mergeTexts<T extends Tree>(base: T, extra: Tree | undefined, source: string): T {
  if (!extra) return base
  const out: Tree = { ...base }
  for (const [key, value] of Object.entries(extra)) {
    const current = out[key]
    if (isTree(current) && isTree(value)) out[key] = mergeTexts(current, value, source)
    else {
      if (current !== undefined && import.meta.env.DEV)
        console.warn(`i18n: ${source} replaces the existing text "${key}"`)
      out[key] = value
    }
  }
  return out as T
}

function withFeatures<T extends Tree>(base: T, locale: keyof FeatureTexts): T {
  return Object.entries(featureTexts)
    .sort(([a], [b]) => a.localeCompare(b))
    .reduce((texts, [path, mod]) => mergeTexts(texts, mod[locale], path), base)
}

export const i18n = createI18n({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: 'en',
  messages: { en: withFeatures(en, 'en'), 'zh-CN': withFeatures(zhCN, 'zh') },
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

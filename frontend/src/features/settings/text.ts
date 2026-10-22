import { useI18n } from 'vue-i18n'
import { i18n } from '@/i18n'
import type { SettingValue } from '@/api/types'
import type { Setting } from './api'

const UNIT_KEYS: Record<string, string> = { '/min': 'per_min', '%': 'pct', 'MB/s': 'MBps' }
// Comma-separated settings edited as a multiple choice.
export const MULTI: Record<string, string[]> = { group_events: ['admin_alerts', 'approvals', 'announcements'] }

/** A text of the other UI language too: search finds a setting by its English and its Chinese label. */
function textIn(locale: 'en' | 'zh-CN', key: string): string {
  const g = i18n.global
  return g.te(key, locale) ? String(g.t(key, {}, { locale })) : ''
}

const HAYSTACKS = new Map<string, string>()

export const splitList = (v: SettingValue | null | undefined) =>
  typeof v === 'string' && v ? v.split(',').map((x) => x.trim()) : []

/** Labels, descriptions and how values read, for settings of the registry (F07). */
export function useSettingText() {
  const { t, te } = useI18n()

  const label = (s: Pick<Setting, 'key'>) => (te(`admin.settings.${s.key}`) ? t(`admin.settings.${s.key}`) : s.key)
  const desc = (s: Pick<Setting, 'key'>) => (te(`admin.settings.${s.key}_desc`) ? t(`admin.settings.${s.key}_desc`) : '')
  const unit = (s: Pick<Setting, 'unit'>) => {
    if (!s.unit) return ''
    const key = `settingsUi.units.${UNIT_KEYS[s.unit] ?? s.unit}`
    return te(key) ? t(key) : s.unit
  }
  const groupLabel = (g: string) => (te(`admin.settings.groups.${g}`) ? t(`admin.settings.groups.${g}`) : g)
  const cardLabel = (c: string) => (te(`settingsUi.cards.${c}`) ? t(`settingsUi.cards.${c}`) : groupLabel(c))

  const multiOptions = (key: string) =>
    (MULTI[key] ?? []).map((v) => ({ label: t(`admin.channels.kinds.${v}`), value: v }))
  const choiceOptions = (s: Setting) =>
    (s.choices ?? []).map((c) => {
      const key = `admin.settings.choice.${s.key}.${c}`
      return { label: te(key) ? t(key) : `${c} ${unit(s)}`.trim(), value: c }
    })

  /** A value as people read it: choices and lists by their labels, 0 by its meaning, never a secret. */
  function display(s: Setting, v: SettingValue | null | undefined): string {
    if (v === null || v === undefined) return '—'
    if (s.secret) return v === '' ? t('settingsUi.secretUnset') : '••••••'
    if (typeof v === 'boolean') return v ? t('admin.settings.on') : t('admin.settings.off')
    if (v === '') return t('admin.settings.notSet')
    if (v === 0 && te(`settingsUi.zero.${s.key}`)) return t(`settingsUi.zero.${s.key}`)
    if (MULTI[s.key]) {
      const labels = multiOptions(s.key).filter((o) => splitList(v).includes(o.value)).map((o) => o.label)
      return labels.length ? labels.join(', ') : t('admin.settings.notSet')
    }
    if (s.choices) return choiceOptions(s).find((o) => o.value === v)?.label ?? `${v}`
    const u = unit(s)
    return u ? `${v} ${u}` : `${v}`
  }

  /** Everything a search may match: key, variable, keywords, labels and descriptions in both languages. */
  function haystack(s: Setting): string {
    const cached = HAYSTACKS.get(s.key)
    if (cached) return cached
    const keys = [`admin.settings.${s.key}`, `admin.settings.${s.key}_desc`, `admin.settings.groups.${s.group}`]
    const texts = (['en', 'zh-CN'] as const).flatMap((l) => keys.map((k) => textIn(l, k)))
    const hay = [s.key, s.env_var, ...s.keywords, ...texts].join('\n').toLowerCase()
    HAYSTACKS.set(s.key, hay)
    return hay
  }

  return { label, desc, unit, groupLabel, cardLabel, display, choiceOptions, multiOptions, haystack }
}

/** Whether a setting applies under the values being edited ("key" = that setting is on / set, "key=value"). */
export function dependencyMet(dep: string | null, value: (key: string) => SettingValue | null | undefined): boolean {
  if (!dep) return true
  const [key, want] = dep.split('=')
  const v = value(key)
  if (want !== undefined) return String(v) === want
  return v !== false && v !== '' && v !== 0 && v !== null && v !== undefined
}

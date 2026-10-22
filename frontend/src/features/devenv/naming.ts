// Names of environments on cards, and their SSH config entries (F04 naming, F10 unique Host aliases).
import type { Reservation } from '@/api/types'
import type { DevSessionX } from './api'

/** The part of an SSH Host alias / file name made from a free-text name: lower case, [a-z0-9-] only. */
export function slug(name: string): string {
  const s = name
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
  return s || 'env'
}

/**
 * Card titles of open environments: a kept environment by its name; one from a template by its booking's note,
 * else the template's name, numbered from the second one on ("PyTorch", "PyTorch #2").
 */
export function environmentTitles(sessions: DevSessionX[], bookings: Reservation[] = []): Map<number, string> {
  const notes = new Map(bookings.filter((r) => r.note?.trim()).map((r) => [r.id, r.note.trim()]))
  const out = new Map<number, string>()
  const seen = new Map<string, number>()
  for (const s of [...sessions].sort((a, b) => a.id - b.id)) {
    if (s.kind === 'shared') {
      out.set(s.id, s.lane_name ?? s.template_name ?? s.image)
      continue
    }
    if (s.project_name) {
      out.set(s.id, s.project_name)
      continue
    }
    const note = s.reservation_id !== null ? notes.get(s.reservation_id) : undefined
    if (note) {
      out.set(s.id, note)
      continue
    }
    const base = s.template_name ?? s.image.split('/').pop() ?? s.image
    const n = (seen.get(base) ?? 0) + 1
    seen.set(base, n)
    out.set(s.id, n === 1 ? base : `${base} #${n}`)
  }
  return out
}

/** One Host alias per environment ("slotserve-thesis"), unique among the given titles. */
export function sshAliases(titles: Map<number, string>): Map<number, string> {
  const out = new Map<number, string>()
  const used = new Set<string>()
  for (const [id, title] of [...titles.entries()].sort((a, b) => a[0] - b[0])) {
    let alias = `slotserve-${slug(title)}`
    if (used.has(alias)) alias = `${alias}-${id}`
    used.add(alias)
    out.set(id, alias)
  }
  return out
}

/**
 * The ~/.ssh/config entry of an environment. HostKeyAlias keeps each environment's own host key apart in
 * known_hosts, so a port that another environment used before does not look like a changed host.
 */
export function sshConfig(alias: string, host: string, port: number | null, user: string | null): string {
  return [
    `Host ${alias}`,
    `  HostName ${host}`,
    `  Port ${port ?? 22}`,
    `  User ${user ?? 'root'}`,
    `  HostKeyAlias ${alias}`,
  ].join('\n')
}

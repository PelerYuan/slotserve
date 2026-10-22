// Display names for machines, GPUs and lanes, so the same thing is never shown twice ("GPU 0 · GPU 0").
//
// Rules (F14):
// - a GPU is "GPU n"; several are "GPU 0,1"; a lane without a GPU list uses every visible GPU ("All visible GPUs");
// - a lane's GPU list is shown next to its name only when the name does not already say it;
// - names are joined with " · " and a part equal to the one before it is dropped;
// - with several machines a name gets the machine first ("machine · GPU n"); with one machine the machine is left out.

type Translate = (key: string, params?: Record<string, unknown>) => string

const SEP = ' · '

/** Joins name parts with " · ", skipping empty parts and parts that repeat the one before them. */
export function joinName(...parts: (string | null | undefined)[]): string {
  const out: string[] = []
  for (const part of parts) {
    const p = (part ?? '').trim()
    if (p && p !== out[out.length - 1]) out.push(p)
  }
  return out.join(SEP)
}

/** "GPU 3". */
export function gpuName(index: number | string): string {
  return `GPU ${index}`
}

/** "GPU 0,1" for a GPU list; an empty list means every GPU the machine has. */
export function gpuListName(gpus: readonly number[] | null | undefined, t: Translate): string {
  return gpus?.length ? gpuName(gpus.join(',')) : t('naming.allGpus')
}

/** True when a lane's name already says which GPUs it has ("GPU 0" for GPU 0, "GPU 0,1" for GPUs 0 and 1). */
function nameSaysGpus(name: string, gpus: readonly number[]): boolean {
  const norm = (s: string) => s.toLowerCase().replace(/\s+/g, '')
  return norm(name) === norm(gpuName(gpus.join(',')))
}

/** The GPU list to show next to a lane's name, or '' when the name already says it or the lane has no list. */
export function laneGpus(lane: { name: string; gpus?: readonly number[] | null }): string {
  const gpus = lane.gpus ?? []
  if (!gpus.length || nameSaysGpus(lane.name, gpus)) return ''
  return gpuName(gpus.join(','))
}

/** A lane's full name: "[machine · ]name[ · GPU list]". */
export function laneLabel(
  lane: { name: string; gpus?: readonly number[] | null },
  machine?: string | null,
): string {
  return joinName(machine, lane.name, laneGpus(lane))
}

/** One GPU, optionally with the lane it belongs to: "GPU 0" or "GPU 0 · lane Training". */
export function gpuInLane(index: number, laneName: string | null | undefined, t: Translate, machine?: string | null): string {
  const gpu = gpuName(index)
  const lane = laneName && laneName.trim() !== gpu ? t('naming.inLane', { lane: laneName.trim() }) : ''
  return joinName(machine, gpu, lane)
}

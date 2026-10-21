""" "Pick for me": which machine (and GPU lane) fits a request best.

Given what someone needs - a GPU or not, GPU memory, CPU cores, RAM, a model - every machine gets a score from 0 to
100 and a list of reasons. Machines that cannot serve the request (offline when it has to start now, too little GPU
memory, the lane is booked in the requested time, too few free cores ...) are kept in the list with ``fits`` False,
so the page can explain why; the best fitting candidate comes first. The choice is only a suggestion: people can
always pick another machine.

Preferences, in order: the request fits; the machine is online; the most free capacity of the kind asked for; low
current load (busy machines are avoided); GPU machines' CPUs are left to GPU users when a CPU-only machine fits as
well.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .machines import BUSY_CPU_PERCENT, MachineLaneOut, MachineOut

PURPOSES = ("booking", "cpu_env", "deploy", "any")


@dataclass
class Need:
    gpu: bool | None = None  # True: a GPU lane; False: CPU only; None: whatever fits best
    min_gpu_memory_mb: int = 0
    cores: float = 0.0
    memory_mb: int = 0
    model_id: int | None = None  # the machine must have this model's files
    purpose: str = "any"  # booking: a lane free in the window; cpu_env: start now; deploy: lanes that fit
    taken_lanes: set[int] = field(default_factory=set)  # lanes booked in the requested window (booking)


@dataclass
class Candidate:
    node_id: int
    node_name: str
    lane_id: int | None
    lane_name: str | None
    score: float
    fits: bool
    reasons: list[str]  # why it does not fit, or what counts against it


def _frac(part: float | None, whole: float | None) -> float:
    if not whole or part is None:
        return 0.0
    return max(0.0, min(1.0, part / whole))


def _load_penalty(m: MachineOut, reasons: list[str]) -> float:
    if m.cpu_percent is None:
        return 0.0
    if m.cpu_percent >= BUSY_CPU_PERCENT:
        reasons.append("busy")
        return 15.0
    return 0.0


def _common(m: MachineOut, need: Need, reasons: list[str]) -> bool:
    """Checks every kind of request shares; returns whether the machine can still fit."""
    ok = True
    if m.sharing_paused:
        reasons.append("sharing_paused")
        ok = False
    if not m.online:
        reasons.append("offline")
        if need.purpose in ("cpu_env",):
            ok = False
    if need.model_id is not None and not any(x.id == need.model_id for x in m.models):
        reasons.append("model_missing")
        ok = False
    return ok


def _gpu_candidate(m: MachineOut, lane: MachineLaneOut, need: Need) -> Candidate:
    reasons: list[str] = []
    fits = _common(m, need, reasons)
    if need.min_gpu_memory_mb:
        if lane.gpu_memory_mb is None:
            reasons.append("gpu_memory_unknown")
        elif lane.gpu_memory_mb < need.min_gpu_memory_mb:
            reasons.append("gpu_memory_short")
            fits = False
    free_in_window = True
    if need.purpose == "booking":
        free_in_window = lane.id not in need.taken_lanes
        if not free_in_window:
            reasons.append("lane_booked")
            fits = False
    elif need.purpose != "deploy" and not lane.free_now:
        reasons.append("lane_busy")
    # scoring: free lane, free GPU memory, idle GPUs, idle CPU; offline machines sink
    score = 0.0
    score += 35.0 if (free_in_window if need.purpose == "booking" else lane.free_now) else 0.0
    if lane.gpu_memory_mb:
        used = lane.gpu_memory_used_mb if lane.gpu_memory_used_mb is not None else 0
        spare = lane.gpu_memory_mb - (used if lane.free_now or need.purpose == "deploy" else lane.gpu_memory_mb)
        score += 30.0 * _frac(max(0, spare), max(lane.gpu_memory_mb, need.min_gpu_memory_mb or 1))
    gpu_utils = [g.utilization_pct for g in m.gpus if g.lane_id == lane.id and g.utilization_pct is not None]
    score += 15.0 * (1.0 - (sum(gpu_utils) / len(gpu_utils) / 100.0 if gpu_utils else 0.0))
    score += 10.0 * (1.0 - _frac(m.cpu_percent, 100.0))
    if need.memory_mb and m.capacity.memory_free_mb is not None and m.capacity.memory_free_mb < need.memory_mb:
        reasons.append("memory_short")
        fits = False
    score += 10.0 if m.online else -50.0
    score -= _load_penalty(m, reasons)
    return Candidate(m.id, m.name, lane.id, lane.name, round(max(0.0, min(100.0, score)), 1), fits, reasons)


def _cpu_candidate(m: MachineOut, need: Need) -> Candidate:
    reasons: list[str] = []
    fits = _common(m, need, reasons)
    cap = m.capacity
    if need.purpose == "cpu_env" and not cap.cpu_envs:
        reasons.append("cpu_envs_unsupported")
        fits = False
    if need.cores:
        if cap.cpu_cores_free is None:
            reasons.append("cores_unknown")
        elif cap.cpu_cores_free < need.cores:
            reasons.append("cores_short")
            fits = False
    if need.memory_mb:
        if cap.memory_free_mb is None:
            reasons.append("memory_unknown")
        elif cap.memory_free_mb < need.memory_mb:
            reasons.append("memory_short")
            fits = False
    score = 0.0
    score += 35.0 * _frac(cap.cpu_cores_free, cap.cpu_cores)
    score += 25.0 * _frac(cap.memory_free_mb, cap.memory_total_mb)
    score += 20.0 * (1.0 - _frac(m.cpu_percent, 100.0))
    score += 10.0 if not m.gpus else 0.0  # keep GPU machines' cores for whoever books their GPUs
    score += 10.0 if m.online else -50.0
    score -= _load_penalty(m, reasons)
    return Candidate(m.id, m.name, None, None, round(max(0.0, min(100.0, score)), 1), fits, reasons)


def candidates(machines: list[MachineOut], need: Need) -> list[Candidate]:
    """Every option (one per GPU lane, one per machine for CPU work), best first."""
    out: list[Candidate] = []
    for m in machines:
        if need.gpu is not False:
            out += [_gpu_candidate(m, lane, need) for lane in m.lanes]
            if need.gpu is True and not m.lanes:
                reasons = ["no_gpu"]
                _common(m, need, reasons)
                out.append(Candidate(m.id, m.name, None, None, 0.0, False, reasons))
        if need.gpu is not True:
            out.append(_cpu_candidate(m, need))
    return sorted(out, key=lambda c: (not c.fits, -c.score, c.node_name, c.lane_id or 0))


def best_per_machine(options: list[Candidate]) -> list[Candidate]:
    """The best option of every machine, in the order of ``options``."""
    seen: set[int] = set()
    out = []
    for c in options:
        if c.node_id not in seen:
            seen.add(c.node_id)
            out.append(c)
    return out


def recommend(machines: list[MachineOut], need: Need) -> tuple[Candidate | None, list[Candidate]]:
    """(the best fitting option or None, every option best first)."""
    options = candidates(machines, need)
    best = next((c for c in options if c.fits), None)
    return best, options

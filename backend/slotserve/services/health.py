"""Health checks, background task supervision and alerts (architecture A05).

One registry of checks feeds every view of the system's health: ``/api/health`` (``checks[]``), the admin's
system health table and setup checklist (checks with ``onboarding=True``), ``/metrics``
(``slotserve_check_status``) and ``slotserve doctor``, which runs the same registry in its own process.

- ``register_check(id, group=..., run=..., every_s=...)``: ``run(ctx)`` returns a ``CheckResult`` (or ``None``
  when the check does not apply, e.g. no storage configured). Built-in checks live in ``health_checks.py``.
- ``report(component, status, detail)``: event-type results pushed by a module (a node's state, a failed
  operation), handled exactly like a check result.
- ``supervised(name, run)``: wraps a background loop (``main.py``, one line per task). It records when the task
  stops or crashes (and restarts it after a pause when given the loop function rather than a coroutine), and
  attributes errors the loop logs while it keeps running, so a loop that swallows an exception every tick shows up
  as failing. ``heartbeat(name)`` lets a loop say it finished a round; a loop that does stops being "stalled" only
  through that.
- Alerts are level changes, not results: a check reaching its ``alert_level`` (error by default; after
  ``alert_after_s``) sends ``check_alert`` once; leaving it sends ``check_recovered``. Both go through the
  notifier, i.e. into every admin's in-app inbox first and then to chat platforms. ``check_state`` keeps the
  level, since when, whether admins were alerted (no repeats across restarts) and a dismissal with its reason.
"""

import asyncio
import contextlib
import contextvars
import inspect
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..db import SessionLocal, utcnow
from ..orm import CheckState

log = logging.getLogger(__name__)

LEVELS = ("ok", "info", "warn", "error")
RANK = {name: i for i, name in enumerate(LEVELS)}
# report(): the node/relay vocabulary (ok | degraded | down) maps onto check levels
STATUS_LEVEL = {"ok": "ok", "info": "info", "warn": "warn", "degraded": "warn", "error": "error", "down": "error"}
TICK_SECONDS = 5.0
CHECK_TIMEOUT = 20.0
TASK_ERROR_WINDOW = timedelta(minutes=10)
TASK_ERRORS_FAILING = 3  # errors within the window that make a running task "failing"
RESTART_DELAYS = (5, 15, 60, 300)  # seconds before restarting a crashed loop, then every 5 minutes


@dataclass
class CheckResult:
    """``title_key`` names the sentence that describes the outcome (``TITLES``, frontend ``checks.<key>``);
    ``params`` fill it. ``evidence`` is raw text (log lines, error messages); ``actions`` are buttons/links
    ``{kind: link|setting|retry|docs, target}``."""

    level: str
    title_key: str
    params: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""
    actions: list[dict[str, str]] = field(default_factory=list)
    dismissible: bool = False
    diagnosis: dict | None = None
    id: str = ""

    def __post_init__(self):
        if self.level not in RANK:
            raise ValueError(f"unknown check level {self.level!r}")


@dataclass
class Ctx:
    """What a check may look at. ``state`` is the app state in the server and ``None`` in ``slotserve doctor``."""

    settings: Any
    state: Any = None
    docker: Any = None
    now: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Check:
    id: str
    group: str
    run: Callable[[Ctx], Awaitable[CheckResult | None]]
    every_s: float = 60
    onboarding: bool = False
    doc_anchor: str = ""
    alert: bool = True
    alert_level: str = "error"
    alert_after_s: float = 0
    server_only: bool = False  # needs the running server (lanes, tasks): skipped by `slotserve doctor`


REGISTRY: dict[str, Check] = {}
TITLES: dict[str, tuple[str, str]] = {}  # title key -> (English, Chinese) with {param} placeholders


def _pattern_library(evidence: str) -> dict | None:
    """The error pattern library (services/diagnostics): a diagnosis only when a pattern matched."""
    from .diagnostics import diagnose

    d = diagnose(evidence)
    return d.model_dump(mode="json") if d.recognized else None


_diagnoser: Callable[[str], dict | None] | None = _pattern_library


def register_check(
    id: str,
    *,
    group: str,
    run: Callable[[Ctx], Awaitable[CheckResult | None]],
    every_s: float = 60,
    onboarding: bool = False,
    doc_anchor: str = "",
    alert: bool = True,
    alert_level: str = "error",
    alert_after_s: float = 0,
    server_only: bool = False,
) -> Check:
    """Adds (or replaces) a check. ``alert=False`` for checks whose failures another event already announces."""
    check = Check(id, group, run, every_s, onboarding, doc_anchor, alert, alert_level, alert_after_s, server_only)
    REGISTRY[id] = check
    return check


def register_titles(titles: dict[str, tuple[str, str]]) -> None:
    TITLES.update(titles)


def set_diagnoser(fn: Callable[[str], dict | None] | None) -> None:
    """Replaces what attaches a diagnosis to error checks with evidence (default: the error pattern library)."""
    global _diagnoser
    _diagnoser = fn


class _Params(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def title(key: str, params: dict | None = None, locale: str = "en") -> str:
    en, zh = TITLES.get(key, (key, key))
    template = zh if (locale or "").startswith("zh") else en
    values = _Params({k: _plain(v) for k, v in (params or {}).items()})
    try:
        return template.format_map(values)
    except (ValueError, IndexError):
        return template


def _plain(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    return value


# ---- event-type reports ------------------------------------------------------------------------------------

_reported: dict[str, tuple[CheckResult, str, bool]] = {}  # component -> (result, group, alert)
_wake: asyncio.Event | None = None


def report(
    component: str,
    status: str,
    detail: str = "",
    *,
    alert: bool | None = None,
    title_key: str | None = None,
    params: dict | None = None,
    group: str = "events",
) -> None:
    """An event-type result (``status``: ok | info | warn | error, or ok | degraded | down). It appears as the
    check ``component`` and alerts like one (``alert=False`` to only show it). ``status`` ok with no detail and
    no earlier report is ignored."""
    level = STATUS_LEVEL.get(status)
    if level is None:
        raise ValueError(f"unknown status {status!r}")
    key = title_key or ("component_ok" if level == "ok" else "component_problem")
    result = CheckResult(level, key, {"component": component, **(params or {})}, evidence=detail or "", id=component)
    _reported[component] = (result, group, True if alert is None else alert)
    if _wake is not None:
        _wake.set()


def forget(component: str) -> None:
    """Removes an event-type component (e.g. a deleted node)."""
    _reported.pop(component, None)
    if _wake is not None:
        _wake.set()


# ---- background tasks --------------------------------------------------------------------------------------


@dataclass
class TaskInfo:
    name: str
    started_at: datetime
    state: str = "running"  # running | restarting | stopped | failed | cancelled
    restarts: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    errors: deque = field(default_factory=lambda: deque(maxlen=50))
    last_beat: datetime | None = None
    beat_every: float | None = None

    def recent_errors(self, now: datetime) -> int:
        return sum(1 for t in self.errors if now - t <= TASK_ERROR_WINDOW)


TASKS: dict[str, TaskInfo] = {}
_current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar("slotserve_task", default=None)


def heartbeat(name: str | None = None, *, every_s: float | None = None) -> None:
    """Called by a loop each round. ``every_s`` is its usual interval: no round for 3 × that (at least 2 minutes)
    makes the task "stalled". Without a name, the supervised task it runs in."""
    name = name or _current_task.get()
    if not name:
        return
    info = TASKS.get(name)
    if info is None:
        info = TASKS[name] = TaskInfo(name, utcnow())
    info.last_beat = utcnow()
    if every_s:
        info.beat_every = every_s


def _note_error(name: str, message: str) -> None:
    info = TASKS.get(name)
    if info is None:
        return
    now = utcnow()
    info.last_error, info.last_error_at = message[:1000], now
    info.errors.append(now)


class _ErrorTap(logging.Handler):
    """Errors logged by code running inside a supervised task count against that task."""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        name = _current_task.get()
        if not name or record.name == __name__:
            return
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            message = f"{message}: {type(exc).__name__}: {exc}"
        _note_error(name, message)


_tap = _ErrorTap()


def _install_tap() -> None:
    root = logging.getLogger("slotserve")
    if _tap not in root.handlers:
        root.addHandler(_tap)


async def supervised(name: str, run: Callable[[], Coroutine] | Coroutine) -> None:
    """Runs a background loop and keeps track of it for the ``tasks`` check.

    ``run`` is the loop function (``obj.run``), so a crashed loop is restarted after a pause, or a coroutine
    (``obj.run()``), which can only run once. Cancelling the task (shutdown) cancels the loop.
    """
    _install_tap()
    factory = run if callable(run) and not inspect.iscoroutine(run) else None
    info = TASKS[name] = TaskInfo(name, utcnow())
    _current_task.set(name)  # this task's own context: errors logged inside are attributed to it
    coro = factory() if factory is not None else run
    while True:
        try:
            await coro
            info.state = "stopped"
            log.warning("background task %s stopped", name)
        except asyncio.CancelledError:
            info.state = "cancelled"
            raise
        except Exception as exc:
            info.state = "failed"
            log.exception("background task %s crashed", name)
            _note_error(name, f"{type(exc).__name__}: {exc}")
        if factory is None:
            return
        delay = RESTART_DELAYS[min(info.restarts, len(RESTART_DELAYS) - 1)]
        info.restarts += 1
        await asyncio.sleep(delay)
        info.state = "running"
        info.started_at = utcnow()
        coro = factory()


def reset_runtime_state() -> None:
    """Forgets tasks and reports (a new app in the same process, i.e. tests)."""
    TASKS.clear()
    _reported.clear()


# ---- the monitor -------------------------------------------------------------------------------------------


class HealthMonitor:
    """Runs due checks every few seconds, keeps their latest results, persists level changes and alerts.

    In ``slotserve doctor`` (``persist=False``) it only runs checks: no state is written and nobody is alerted.
    """

    def __init__(self, settings, state: Any = None, docker: Any = None, *, persist: bool = True):
        from . import health_checks  # noqa: F401  (registers the built-in checks)

        global _wake
        self.settings = settings
        self.state = state
        self.docker = docker
        self.persist = persist
        self.results: dict[str, CheckResult] = {}
        self.last_run: dict[str, datetime] = {}
        self.states: dict[str, CheckState] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        if persist:
            _wake = self._wake

    # -- running -------------------------------------------------------------

    def ctx(self) -> Ctx:
        docker = self.docker
        if docker is None and self.state is not None and getattr(self.state, "devenv", None) is not None:
            docker = self.state.devenv.docker
        return Ctx(self.settings, self.state, docker)

    def checks(self) -> list[Check]:
        return [c for c in REGISTRY.values() if self.state is not None or not c.server_only]

    async def run(self) -> None:
        while True:
            try:
                await self.run_checks()
                heartbeat("health", every_s=TICK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("health checks failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)

    def poke(self) -> None:
        self._wake.set()

    async def run_checks(self, *, force: bool = False, ids: set[str] | None = None) -> dict[str, CheckResult]:
        """Runs the checks that are due (all with ``force``; only ``ids`` if given) and records the outcome."""
        async with self._lock:
            now = utcnow()
            ctx = self.ctx()
            due = [
                c
                for c in self.checks()
                if (ids is None or c.id in ids)
                and (force or c.id not in self.last_run or (now - self.last_run[c.id]).total_seconds() >= c.every_s)
            ]
            outcomes = await asyncio.gather(*(self._run_one(c, ctx) for c in due))
            changes: dict[str, tuple[CheckResult | None, Check | None]] = {}
            for check, result in zip(due, outcomes, strict=True):
                self.last_run[check.id] = now
                changes[check.id] = (result, check)
            for component, (result, group, alert) in list(_reported.items()):
                if ids is None or component in ids:
                    check = Check(component, group, _never, alert=alert)
                    changes[component] = (result, check)
            for gone in [k for k in self.results if k not in REGISTRY and k not in _reported]:
                changes[gone] = (None, None)
            for cid, (result, _check) in changes.items():
                if result is None:
                    self.results.pop(cid, None)
                else:
                    self.results[cid] = result
            if self.persist:
                await self._record(changes, now)
            return {cid: r for cid, (r, _c) in changes.items() if r is not None}

    async def _run_one(self, check: Check, ctx: Ctx) -> CheckResult | None:
        try:
            result = await asyncio.wait_for(check.run(ctx), CHECK_TIMEOUT)
        except TimeoutError:
            result = CheckResult("error", "check_timeout", {"check": check.id, "seconds": int(CHECK_TIMEOUT)})
        except Exception as exc:
            log.exception("health check %s failed", check.id)
            result = CheckResult("error", "check_crashed", {"check": check.id}, evidence=f"{type(exc).__name__}: {exc}")
        if result is None:
            return None
        result.id = check.id
        if result.level == "error" and result.diagnosis is None and result.evidence and _diagnoser is not None:
            try:
                result.diagnosis = _diagnoser(result.evidence)
            except Exception:
                log.exception("diagnosing %s failed", check.id)
        return result

    # -- state and alerts ----------------------------------------------------

    async def load(self) -> None:
        if self._loaded or not self.persist:
            return
        async with SessionLocal() as db:
            self.states = {s.id: s for s in (await db.execute(select(CheckState))).scalars()}
        self._loaded = True

    async def _record(self, changes: dict, now: datetime) -> None:
        await self.load()
        alerts: list[tuple[str, dict]] = []
        dirty: list[CheckState] = []
        removed: list[str] = []
        for cid, (result, check) in changes.items():
            st = self.states.get(cid)
            if result is None:
                if st is not None:
                    if st.alerted_at is not None:
                        alerts.append(("check_recovered", self._alert_data(cid, st.title_key or cid, {}, "")))
                    removed.append(cid)
                    del self.states[cid]
                continue
            params_json = json.dumps(result.params, sort_keys=True, default=str)
            if st is None:
                st = CheckState(id=cid, level=result.level, since=now)
                self.states[cid] = st
                dirty.append(st)
            changed = st.level != result.level
            if changed:
                st.level, st.since = result.level, now
                st.dismissed_by = st.dismissed_reason = st.dismissed_at = None  # a dismissal ends with its level
            if changed or st.title_key != result.title_key or st.params_json != params_json:
                st.title_key, st.params_json = result.title_key, params_json
                st.evidence = (result.evidence or "")[:4000]
                if st not in dirty:
                    dirty.append(st)
            st.last_run = now
            if check is None or not check.alert:
                continue
            alarming = RANK[result.level] >= RANK[check.alert_level]
            if alarming and st.alerted_at is None and st.dismissed_at is None:
                if (now - _aware(st.since)).total_seconds() >= check.alert_after_s:
                    st.alerted_at = now
                    alerts.append(("check_alert", self._alert_data(cid, result.title_key, result.params, result)))
                    if st not in dirty:
                        dirty.append(st)
            elif not alarming and st.alerted_at is not None:
                st.alerted_at = None
                alerts.append(("check_recovered", self._alert_data(cid, result.title_key, result.params, result)))
                if st not in dirty:
                    dirty.append(st)
        if dirty or removed:
            async with SessionLocal() as db:
                for st in dirty:
                    await db.merge(st)
                if removed:
                    await db.execute(delete(CheckState).where(CheckState.id.in_(removed)))
                await db.commit()
        for event, data in alerts:
            self._emit(event, data)

    def _alert_data(self, cid: str, key: str, params: dict, result: CheckResult | str) -> dict:
        evidence = result.evidence if isinstance(result, CheckResult) else ""
        detail = (evidence or "").strip().splitlines()[-1][:300] if (evidence or "").strip() else ""
        level = result.level if isinstance(result, CheckResult) else "ok"
        return {
            "check": cid,
            "level": level,
            "title_key": key,
            "title_en": title(key, params, "en"),
            "title_zh": title(key, params, "zh-CN"),
            "detail": f" ({detail})" if detail else "",
        }

    def _emit(self, event: str, data: dict) -> None:
        notifier = getattr(self.state, "notifier", None) if self.state is not None else None
        if notifier is not None:
            notifier.emit(event, **data)

    async def dismiss(self, cid: str, user_id: int, reason: str) -> CheckState:
        await self.load()
        st = self.states.get(cid)
        if st is None:
            raise KeyError(cid)
        st.dismissed_by, st.dismissed_reason, st.dismissed_at = user_id, reason, utcnow()
        async with SessionLocal() as db:
            await db.merge(st)
            await db.commit()
        from .health_checks import ON_DISMISS

        if (hook := ON_DISMISS.get(cid)) is not None:
            hook(self.settings)
        return st

    async def undismiss(self, cid: str) -> CheckState:
        await self.load()
        st = self.states.get(cid)
        if st is None:
            raise KeyError(cid)
        st.dismissed_by = st.dismissed_reason = st.dismissed_at = None
        async with SessionLocal() as db:
            await db.merge(st)
            await db.commit()
        return st

    # -- views ---------------------------------------------------------------

    def items(self, locale: str = "en") -> list[dict[str, Any]]:
        """Every current result, ordered by severity then group, as ``/api/health`` shows it."""
        out = []
        for cid, r in self.results.items():
            check = REGISTRY.get(cid)
            group = check.group if check else (_reported.get(cid, (None, "events"))[1])
            st = self.states.get(cid)
            dismissed = None
            if st is not None and st.dismissed_at is not None:
                dismissed = {"by": st.dismissed_by, "reason": st.dismissed_reason or "", "at": st.dismissed_at}
            out.append(
                {
                    "id": cid,
                    "group": group,
                    "level": r.level,
                    "title_key": r.title_key,
                    "title": title(r.title_key, r.params, locale),
                    "params": r.params,
                    "evidence": r.evidence,
                    "actions": r.actions,
                    "dismissible": r.dismissible,
                    "dismissed": dismissed,
                    "diagnosis": r.diagnosis,
                    "onboarding": bool(check and check.onboarding),
                    "doc_anchor": check.doc_anchor if check else "",
                    "since": st.since if st is not None else None,
                    "last_run": self.last_run.get(cid) or (st.last_run if st is not None else None),
                    "alerted": bool(st is not None and st.alerted_at is not None),
                }
            )
        out.sort(key=lambda c: (-RANK[c["level"]], c["group"], c["id"]))
        return out

    def problems(self) -> list[str]:
        """English one-liners for every error that is not dismissed (``/api/health`` ``problems[]``)."""
        out = []
        for cid, r in self.results.items():
            st = self.states.get(cid)
            if r.level == "error" and not (st is not None and st.dismissed_at is not None):
                out.append(title(r.title_key, r.params, "en"))
        return sorted(out)

    def worst(self) -> str:
        levels = [
            r.level
            for cid, r in self.results.items()
            if not ((st := self.states.get(cid)) is not None and st.dismissed_at is not None)
        ]
        return max(levels, key=RANK.__getitem__, default="ok")


async def _never(_ctx: Ctx) -> None:  # stand-in run() for reported components
    return None


def _aware(dt: datetime) -> datetime:
    from datetime import UTC

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

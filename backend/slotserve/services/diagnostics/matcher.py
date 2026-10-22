"""Turns failure output (an error message, log lines, an exit code) into a diagnosis: cause, next steps, evidence.

The patterns live in ``patterns.json`` next to this file (data, not code: a new pattern needs no Python change, only
a real log sample in ``tests/samples/logs/``). The first pattern with a matching regex wins; exit codes are only
consulted when no regex matched. Nothing is ever hidden: every diagnosis carries the raw exit code, the error and
the log tail, and an unrecognized failure is a diagnosis with ``recognized=False`` that shows exactly those.

Pure and fast: the standard library plus pydantic, patterns compiled once, results cached.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

PATTERNS_FILE = Path(__file__).with_name("patterns.json")
LOCALES = ("en", "zh-CN")
TAIL_LINES = 20
EVIDENCE_LINES = 3
SCAN_CHARS = 64 * 1024  # only the end of a long log is searched: the error is near the end
LINE_CHARS = 1000
PARAM_CHARS = 200
UNKNOWN = "unknown"

_FLAGS = re.IGNORECASE | re.MULTILINE
_FORMATTER = string.Formatter()
# Light scrubbing of what is shown (evidence, log tail, params); the "copy diagnostics" text still goes through
# the shared redact() of A05.
_SECRETS = (
    (re.compile(r"(authorization:\s*(?:bearer|basic)\s+)\S+", re.I), r"\1***"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}"), "hf_***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"((?:password|passwd|pass|token|secret|api[_-]?key)\s*[=:]\s*)[^\s,;'\"]+", re.I), r"\1***"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1***@"),
)


class DiagnosisText(BaseModel):
    cause: str
    steps: list[str]


class Diagnosis(BaseModel):
    """What went wrong, in both UI languages, with the raw evidence it was derived from."""

    id: str  # pattern id, or "unknown"
    recognized: bool
    category: str  # network, gpu, memory, engine, model, docker, storage, permission, disk, auth, file, unknown
    doc: str | None = None  # docs page and anchor, e.g. "storage.md#types"
    cause_key: str  # i18n key, e.g. diag_port_in_use_cause
    step_keys: list[str]  # diag_port_in_use_step1, ...
    params: dict[str, str]  # values captured from the log, e.g. {"port": "8081"}
    text: dict[str, DiagnosisText]  # rendered texts per locale ("en", "zh-CN")
    evidence: list[str]  # the log lines that matched
    exit_code: int | None = None
    error: str | None = None  # the raw error message, unchanged
    log_tail: list[str]  # the last lines of the log, always kept

    def localized(self, lang: str | None = None) -> DiagnosisText:
        """The texts for a UI language: any "zh*" gets Chinese, everything else English."""
        return self.text["zh-CN" if (lang or "").lower().startswith("zh") else "en"]


@dataclass(frozen=True)
class Pattern:
    id: str
    category: str
    doc: str | None
    regexes: tuple[re.Pattern[str], ...]
    exit_codes: frozenset[int]
    text: dict  # locale -> {"cause": entry, "steps": [entry, ...]}; entry = str | {"text", "generic"}


@dataclass(frozen=True)
class Library:
    patterns: tuple[Pattern, ...]
    unknown: dict  # texts for the fallback, same shape as Pattern.text


def load(path: Path = PATTERNS_FILE) -> Library:
    data = json.loads(path.read_text(encoding="utf-8"))
    patterns = tuple(
        Pattern(
            id=p["id"],
            category=p["category"],
            doc=p.get("doc"),
            regexes=tuple(re.compile(rx, _FLAGS) for rx in p.get("match", ())),
            exit_codes=frozenset(p.get("exit_codes", ())),
            text=p["text"],
        )
        for p in data["patterns"]
    )
    return Library(patterns, data["unknown"])


@lru_cache(maxsize=1)
def library() -> Library:
    return load()


def pattern_ids() -> list[str]:
    return [p.id for p in library().patterns]


def cause_key(pattern_id: str) -> str:
    return f"diag_{pattern_id}_cause"


def step_key(pattern_id: str, n: int) -> str:
    return f"diag_{pattern_id}_step{n}"


def catalog(locale: str) -> dict[str, str]:
    """Every i18n key with its text for one locale (placeholders kept; ``<key>_generic`` for the fallback form)."""
    lib = library()
    out: dict[str, str] = {}
    entries = [(p.id, p.text[locale]) for p in lib.patterns] + [(UNKNOWN, lib.unknown[locale])]
    for pid, texts in entries:
        keyed = [(cause_key(pid), texts["cause"])]
        keyed += [(step_key(pid, n), s) for n, s in enumerate(texts["steps"], 1)]
        for key, entry in keyed:
            if isinstance(entry, str):
                out[key] = entry
            else:
                out[key] = entry["text"]
                out[f"{key}_generic"] = entry["generic"]
    return out


def scrub(text: str) -> str:
    for rx, repl in _SECRETS:
        text = rx.sub(repl, text)
    return text


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in _FORMATTER.parse(text) if name}


def _render(entry: str | dict, params: dict[str, str]) -> str:
    if isinstance(entry, str):
        return entry.format_map(params) if placeholders(entry) <= params.keys() else entry
    text = entry["text"]
    return text.format_map(params) if placeholders(text) <= params.keys() else entry["generic"]


def _texts(texts: dict, params: dict[str, str]) -> dict[str, DiagnosisText]:
    return {
        locale: DiagnosisText(
            cause=_render(texts[locale]["cause"], params),
            steps=[_render(s, params) for s in texts[locale]["steps"]],
        )
        for locale in LOCALES
    }


def normalize_exit_code(code: int | None) -> int | None:
    """Windows NTSTATUS codes may arrive signed (-1073741515); keep them in their usual unsigned form."""
    if code is not None and code < -255:
        return code & 0xFFFFFFFF
    return code


def _as_lines(log: str | Iterable[str] | None) -> list[str]:
    if log is None:
        return []
    if isinstance(log, str):
        return log.splitlines()
    return [line for chunk in log for line in str(chunk).splitlines()]


def _line_at(text: str, start: int, end: int) -> str:
    stop = text.find("\n", end)
    return text[text.rfind("\n", 0, start) + 1 : stop if stop >= 0 else len(text)]


def _clean_param(value: str) -> str:
    return scrub(value.strip().strip(".,;:'\"")[:PARAM_CHARS])


def _match(pattern: Pattern, text: str) -> tuple[list[str], dict[str, str]] | None:
    evidence: list[str] = []
    params: dict[str, str] = {}
    for rx in pattern.regexes:
        m = rx.search(text)
        if m is None:
            continue
        line = _line_at(text, m.start(), m.end())
        if line not in evidence:
            evidence.append(line)
        for name, value in m.groupdict().items():
            if value and name not in params:
                params[name] = _clean_param(value)
    if not evidence:
        return None
    return evidence, params


@lru_cache(maxsize=512)
def _diagnose(error: str | None, log: tuple[str, ...], exit_code: int | None, tail: int) -> Diagnosis:
    lib = library()
    lines = ([error] if error else []) + list(log)
    text = "\n".join(line[:LINE_CHARS] for line in lines)
    if len(text) > SCAN_CHARS:
        text = text[: SCAN_CHARS // 4] + "\n" + text[-SCAN_CHARS:]  # the error message plus the end of the log
    found: tuple[Pattern, list[str], dict[str, str]] | None = None
    for pattern in lib.patterns:
        hit = _match(pattern, text)
        if hit is not None:
            found = (pattern, *hit)
            break
    if found is None and exit_code is not None:
        for pattern in lib.patterns:
            if exit_code in pattern.exit_codes:
                found = (pattern, [], {})
                break
    params_base = {"exit_code": str(exit_code)} if exit_code is not None else {}
    log_tail = [scrub(line[:LINE_CHARS]) for line in log[-tail:]] if tail > 0 else []
    if found is None:
        return Diagnosis(
            id=UNKNOWN,
            recognized=False,
            category=UNKNOWN,
            cause_key=cause_key(UNKNOWN),
            step_keys=[step_key(UNKNOWN, n) for n in range(1, len(lib.unknown["en"]["steps"]) + 1)],
            params=params_base,
            text=_texts(lib.unknown, params_base),
            evidence=[],
            exit_code=exit_code,
            error=error,
            log_tail=log_tail,
        )
    pattern, evidence, params = found
    params = {**params_base, **params}
    return Diagnosis(
        id=pattern.id,
        recognized=True,
        category=pattern.category,
        doc=pattern.doc,
        cause_key=cause_key(pattern.id),
        step_keys=[step_key(pattern.id, n) for n in range(1, len(pattern.text["en"]["steps"]) + 1)],
        params=params,
        text=_texts(pattern.text, params),
        evidence=[scrub(line[:LINE_CHARS]) for line in evidence[:EVIDENCE_LINES]],
        exit_code=exit_code,
        error=error,
        log_tail=log_tail,
    )


def diagnose(
    log: str | Iterable[str] | None = None,
    *,
    error: str | None = None,
    exit_code: int | None = None,
    tail: int = TAIL_LINES,
) -> Diagnosis:
    """Diagnoses a failure from its log (text or lines), its error message and/or its exit code.

    Always returns a Diagnosis; ``recognized`` is False when no pattern matched, and then the texts only say so
    (with the exit code) while ``exit_code``, ``error`` and ``log_tail`` carry the raw information.
    The result is a fresh copy, so callers may change it.
    """
    lines = tuple(_as_lines(log))
    return _diagnose(error or None, lines, normalize_exit_code(exit_code), tail).model_copy(deep=True)

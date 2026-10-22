"""The platform inside a dev environment (F11, A13): which environment it is, how long it has left and how to call a
model - with nothing secret in Docker's environment (R1-11).

* ``SLOTSERVE_ENV_ID/ENV/LANE/GPUS/MACHINE/CHECKPOINT_DIR`` (next to ``SLOTSERVE_USER``/``SLOTSERVE_END`` of the
  session contributor) and ``OPENAI_BASE_URL`` = the container-side gateway (``gateway/container_gw.py``,
  ``http://slotserve-gw:<port>/v1``, only ``/v1``). A template's own ``OPENAI_BASE_URL`` wins.
* The environment key (``sk-ss-env-``, scope ``llm``, revoked when the environment stops) only as the file
  ``/run/slotserve/llm-key`` (0600). ``/etc/profile.d/slotserve-platform.sh`` exports it as ``OPENAI_API_KEY`` in
  login shells, so it never shows in ``docker inspect``, ``/proc/1/environ`` or a saved image's ``Config.Env``.
  Shared environments of idle lanes get no key (people use their own).
* ``/run/slotserve/env``: what changes while the environment runs (the end, when the booking is extended; the
  dev environment manager rewrites it with ``env_file``). ``/run/slotserve/motd.sh``: the login banner, at most
  three lines (environment, GPUs and time left; ``/workspace`` usage; the LLM endpoint and a one-line example).

Values that come from people (template, project and lane names) only ever reach the shell scripts quoted.
"""

import shlex
from datetime import datetime

from ..context import PlatformInfo, SessionContext
from ..model import ContainerSpec

GATEWAY_ALIAS = "slotserve-gw"
KEY_FILE = "run/slotserve/llm-key"
ENV_FILE = "run/slotserve/env"
MOTD_FILE = "run/slotserve/motd.sh"
PROFILE_FILE = "etc/profile.d/slotserve-platform.sh"

PROFILE = """# Added by SlotServe: the platform inside this environment (variables, LLM key, login banner)
if [ -r /run/slotserve/env ]; then . /run/slotserve/env; fi
if [ -z "${OPENAI_API_KEY:-}" ] && [ -r /run/slotserve/llm-key ]; then
  OPENAI_API_KEY="$(cat /run/slotserve/llm-key)"
  export OPENAI_API_KEY
fi
case "$-" in
  *i*)
    if [ -r /run/slotserve/motd.sh ] && [ -z "${SLOTSERVE_MOTD_SHOWN:-}" ]; then
      SLOTSERVE_MOTD_SHOWN=1
      export SLOTSERVE_MOTD_SHOWN
      . /run/slotserve/motd.sh
    fi
    ;;
esac
"""

EXAMPLE = "python3 -c 'from openai import OpenAI; print(OpenAI().models.list())'"

TEXT = {
    "en": {
        "cpu": "CPU only",
        "left": ("", " left, until "),  # before and after the time left
        "ended": "the booking has ended",
        "disk": "/workspace: %s used of %s (%s)\\n",  # awk printf: used, size, percent
        "try": "LLM: {url} · try: {example}",
        "own_key": "LLM: {url} (with your own API key: export OPENAI_API_KEY=sk-ss-...)",
    },
    "zh-CN": {
        "cpu": "仅 CPU",
        "left": ("剩余 ", "，至 "),
        "ended": "预约已结束",
        "disk": "/workspace：已用 %s，共 %s（%s）\\n",
        "try": "LLM：{url} · 示例：{example}",
        "own_key": "LLM：{url}（使用你自己的 API 密钥：export OPENAI_API_KEY=sk-ss-...）",
    },
}


def end_epoch(end_at: str) -> int | None:
    """Seconds since 1970 of an ISO time (naive = UTC), or None."""
    if not end_at:
        return None
    try:
        value = datetime.fromisoformat(end_at)
    except ValueError:
        return None
    if value.tzinfo is None:
        return int((value - datetime(1970, 1, 1)).total_seconds())
    return int(value.timestamp())


def env_file(end_at: str, end_label: str = "") -> bytes:
    """``/run/slotserve/env``: the end of the booking, sourced by login shells (rewritten when it is extended)."""
    lines = ["# Written by SlotServe; rewritten when the booking is extended"]
    epoch = end_epoch(end_at)
    if epoch is not None:
        lines += [
            f"export SLOTSERVE_END={shlex.quote(end_at)}",
            f"export SLOTSERVE_END_EPOCH={epoch}",
            f"export SLOTSERVE_END_LABEL={shlex.quote(end_label or end_at)}",
        ]
    return ("\n".join(lines) + "\n").encode()


def gpu_list(ctx: SessionContext) -> str:
    if ctx.kind == "cpu" or ctx.lane is None or not ctx.lane.devices:
        return ""
    return ",".join(str(d) for d in ctx.lane.devices)


def motd_script(ctx: SessionContext, p: PlatformInfo, with_key: bool) -> bytes:
    """The login banner: at most three lines, the time left computed at login."""
    t = TEXT.get(p.language) or TEXT["en"]
    gpus = gpu_list(ctx)
    head = " · ".join(x for x in (p.env_name, f"GPU {gpus}" if gpus else t["cpu"], p.machine) if x)
    before, after = (shlex.quote(x) if x else "" for x in t["left"])
    lines = [
        "# SlotServe login banner (sourced by /etc/profile.d/slotserve-platform.sh)",
        "_ss_left=",
        'if [ -n "${SLOTSERVE_END_EPOCH:-}" ]; then',
        "  _ss_s=$(( SLOTSERVE_END_EPOCH - $(date +%s) ))",
        '  if [ "$_ss_s" -gt 0 ]; then',
        f'    _ss_left={before}"$(( _ss_s / 3600 ))h$(( _ss_s % 3600 / 60 ))m"{after}"$SLOTSERVE_END_LABEL"',
        "  else",
        f"    _ss_left={shlex.quote(t['ended'])}",
        "  fi",
        "fi",
        f"printf '%s\\n' {shlex.quote(head)}\"${{_ss_left:+ · $_ss_left}}\"",
        "if [ -d /workspace ]; then",
        f"  df -hP /workspace 2>/dev/null | awk -v f={shlex.quote(t['disk'])} 'NR == 2 {{ printf f, $3, $2, $5 }}'",
        "fi",
    ]
    if p.llm_url:
        line = (t["try"] if with_key else t["own_key"]).format(url=p.llm_url, example=EXAMPLE)
        lines.append(f"printf '%s\\n' {shlex.quote(line)}")
    lines.append("unset _ss_left _ss_s")
    return ("\n".join(lines) + "\n").encode()


class PlatformEnvContributor:
    name = "platform_env"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        p = ctx.platform
        if p is None:
            return
        env = spec.env
        env["SLOTSERVE_ENV_ID"] = str(ctx.session_id)
        env["SLOTSERVE_ENV"] = p.env_name
        env["SLOTSERVE_LANE"] = ctx.lane.name if ctx.lane is not None and ctx.kind != "cpu" else ""
        env["SLOTSERVE_GPUS"] = gpu_list(ctx)
        env["SLOTSERVE_MACHINE"] = p.machine
        if ctx.kind != "shared":  # (a shared environment has no /workspace)
            env["SLOTSERVE_CHECKPOINT_DIR"] = p.checkpoint_dir
        with_key = False
        if p.llm_url:
            env["SLOTSERVE_LLM_URL"] = p.llm_url
            env.setdefault("OPENAI_BASE_URL", p.llm_url)  # a template's own endpoint wins
            if p.extra_host:
                spec.host_extra["ExtraHosts"] = [p.extra_host]
            # never Env: docker inspect, /proc/1/environ and docker commit would carry it (R1-11)
            if p.llm_key and ctx.kind != "shared":
                spec.files[KEY_FILE] = (p.llm_key.encode(), 0o600)
                with_key = True
        spec.files[ENV_FILE] = (env_file(ctx.end_at if ctx.kind != "shared" else "", p.end_label), 0o644)
        spec.files[MOTD_FILE] = (motd_script(ctx, p, with_key), 0o644)
        spec.files[PROFILE_FILE] = (PROFILE.encode(), 0o644)

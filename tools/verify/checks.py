"""The checks of a verification run, against a running SlotServe through its public API only.

Order: server -> test account -> CSRF -> GPU booking + LLM -> storage -> each built-in dev environment template
(SSH key and password login, GPUs seen, web interfaces; the first one also checks container hardening and the
web origin isolation) -> CPU-only environment (cores, memory, storage mount, optionally a fork bomb) -> clean-up.

Everything the run creates (a ``verify-<run>`` account, bookings, an API key, a storage entry) is removed at the
end unless ``--keep``. Nothing on the server is reconfigured: a feature that is switched off is reported as skipped.
"""

import asyncio
import contextlib
import re
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .api import Api, ApiFailure, wait_for
from .report import FAIL, MANUAL, PASS, PENDING, SKIP, Report, Result
from .ssh import Ssh

BUILTIN_TEMPLATES = ("pytorch", "jupyter-pytorch", "tensorflow", "cuda", "desktop", "vscode")
GiB = 1024**3
# Capabilities containers/harden.py drops (A01): bit numbers in /proc/self/status CapEff
DROPPED_CAPS = {"NET_RAW": 13, "MKNOD": 27, "SYS_CHROOT": 18, "AUDIT_WRITE": 29, "SETFCAP": 31}


class Outcome(Exception):
    status = FAIL


class Fail(Outcome):
    status = FAIL


class Skip(Outcome):
    status = SKIP


class Pending(Outcome):
    status = PENDING


class Manual(Outcome):
    status = MANUAL


@dataclass
class Options:
    url: str
    admin_user: str
    admin_password: str
    lane: str = ""  # id or name; default: the first lane with GPUs
    model: str = ""  # name; default: the lane's default model, else the first ready one
    templates: tuple[str, ...] = BUILTIN_TEMPLATES
    cpu_template: str = "pytorch"
    storage_id: int | None = None
    storage_host_path: str = ""
    ssh_host: str = ""
    ssh: bool = True
    web: bool = True
    save_image: bool = False
    destructive: bool = False
    keep: bool = False
    load_timeout: float = 900
    devenv_timeout: float = 1800
    booking_minutes: int = 60
    poll: float = 2.0
    verify_tls: bool = True
    workdir: Path = field(default_factory=lambda: Path("."))

    def public(self) -> dict:
        hidden = {"admin_password", "workdir"}
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(self).items() if k not in hidden}


class Verifier:
    def __init__(self, opts: Options, transport: httpx.AsyncBaseTransport | None = None, echo=print):
        self.o = opts
        self.transport = transport
        self.echo = echo
        self.run_id = secrets.token_hex(3)
        host = urlsplit(opts.url).hostname or "unknown"
        self.report = Report(target=opts.url, host=host, options=opts.public())
        self.admin = Api(opts.url, transport, opts.verify_tls)
        self.user: Api | None = None
        self.user_row: dict | None = None
        self.ssh = Ssh(opts.workdir, enabled=opts.ssh)
        self.devenv_password = secrets.token_urlsafe(12)
        self.lanes: list[dict] = []
        self.lane: dict | None = None
        self.devenv: dict = {}
        self.bookings: list[int] = []
        self.cpu_sessions: list[int] = []
        self.key_ids: list[int] = []
        self.created_storage: int | None = None
        self.storage_path: str | None = None
        self.hardening_done = False
        self.web_origin_done = False

    # ---- plumbing ----------------------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def check(self, id: str, title: str) -> AsyncIterator[Result]:
        r = Result(id, title)
        started = time.monotonic()
        self.echo(f"... {id}: {title}")
        try:
            yield r
        except Outcome as o:
            r.status, r.detail = o.status, str(o)
        except (ApiFailure, TimeoutError, AssertionError, OSError, httpx.HTTPError) as exc:
            r.status, r.detail = FAIL, f"{type(exc).__name__}: {exc}"
        r.seconds = time.monotonic() - started
        if r.status == PASS and not r.detail:
            r.detail = "ok"
        self.report.results.append(r)
        self.echo(f"{r.status.upper():>7} {id}: {r.detail}")

    def skipped(self, id: str, title: str, reason: str, status: str = SKIP) -> None:
        self.report.results.append(Result(id, title, status, reason))
        self.echo(f"{status.upper():>7} {id}: {reason}")

    def ssh_target(self, session: dict) -> tuple[str, int, str]:
        host = self.o.ssh_host
        if not host and session.get("ssh_command"):
            host = session["ssh_command"].rsplit("@", 1)[-1]
        return host or self.report.host, int(session["ssh_port"]), session.get("ssh_user") or "root"

    async def book(self, minutes: int, **extra) -> dict:
        now = datetime.now(UTC)
        body = {
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(minutes=minutes)).isoformat(),
            "lane_id": self.lane["id"],
            "note": f"tools/verify {self.run_id}",
            **extra,
        }
        try:
            booking = (await self.user.call("POST", "/api/reservations", json=body))[0]
        except ApiFailure as exc:
            if exc.status == 409:
                raise Fail(f"lane {self.lane['name']} is booked by someone right now ({exc.code}); use --lane") from exc
            raise
        self.bookings.append(booking["id"])
        if booking["status"] == "pending":
            await self.admin.call("POST", f"/api/admin/reservations/{booking['id']}/approve", json={})
        return booking

    async def release(self, booking_id: int) -> None:
        with contextlib.suppress(ApiFailure):
            await self.user.call("DELETE", f"/api/reservations/{booking_id}", expect=(200, 403, 404))
        if booking_id in self.bookings:
            self.bookings.remove(booking_id)

    async def session_for(self, r: Result, match, timeout: float) -> dict:
        """Waits until the matching dev environment runs; fails at once when it failed."""
        last = {"state": None}

        async def probe():
            for s in await self.user.get("/api/devenv/sessions"):
                if match(s):
                    if s["state"] != last["state"]:
                        r.note(f"state {s['state']} {s.get('detail') or ''}".strip())
                        last["state"] = s["state"]
                    if s["state"] == "failed":
                        raise Fail(f"the environment failed: {s.get('error') or s.get('detail')}")
                    return s if s["state"] == "running" else None
            return None

        return await wait_for("the dev environment to run", probe, timeout, self.o.poll)

    # ---- the run -----------------------------------------------------------------------------------------------

    async def run(self) -> Report:
        try:
            if await self.preflight() and await self.setup_account():
                await self.csrf()
                self.csrf_browser()
                await self.gpu_booking()
                await self.storage()
                await self.templates()
                await self.cpu_only()
        finally:
            await self.cleanup()
            await self.admin.close()
            if self.user is not None:
                await self.user.close()
            self.report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        return self.report

    async def preflight(self) -> bool:
        ok = False
        async with self.check("server", "Server reachable, admin sign-in, Docker") as r:
            async with self.admin.client() as c:
                health = await c.get("/healthz")
            assert health.status_code == 200, f"/healthz answered {health.status_code}"
            config = await self.admin.get("/api/config")
            self.report.server_version = config.get("version", "?")
            me = await self.admin.login(self.o.admin_user, self.o.admin_password)
            assert me["is_admin"], f"{self.o.admin_user} is not an admin"
            self.lanes = await self.admin.get("/api/lanes")
            assert self.lanes, "no lanes configured"
            if self.o.lane:
                self.lane = next((x for x in self.lanes if self.o.lane in (str(x["id"]), x["name"])), None)
                assert self.lane is not None, f"no lane {self.o.lane!r}"
            else:
                self.lane = next((x for x in self.lanes if x["gpus"]), self.lanes[0])
            self.devenv = await self.admin.get("/api/devenv")
            docker = self.devenv.get("docker") or {}
            r.detail = (
                f"version {self.report.server_version}; lane {self.lane['name']} (GPUs {self.lane['gpus']}); "
                f"dev environments {'on' if self.devenv.get('enabled') else 'off'}; "
                f"Docker {'ok' if docker.get('ok') else docker.get('error') or 'unknown'}"
            )
            ok = True
        return ok

    async def setup_account(self) -> bool:
        async with self.check("account", "A throw-away test account") as r:
            name = f"verify-{self.run_id}"
            password = secrets.token_urlsafe(16)
            self.user_row = await self.admin.call(
                "POST", "/api/admin/users", json={"username": name, "password": password, "is_admin": False}
            )
            self.user = Api(self.o.url, self.transport, self.o.verify_tls)
            await self.user.login(name, password)
            parts = [f"user {name}"]
            if self.devenv.get("enabled"):
                await self.user.call("PUT", "/api/devenv/password", json={"password": self.devenv_password})
                parts.append("environment password set")
                if (key := await self.ssh.make_key()) is not None:
                    await self.user.call("POST", "/api/ssh-keys", json={"name": "verify", "public_key": key})
                    parts.append("SSH key added")
            r.detail = "; ".join(parts)
        return self.user is not None and bool(self.user.user)

    # ---- L4 security: CSRF (R1-01) -------------------------------------------------------------------------------

    async def csrf(self) -> None:
        async with self.check("security.csrf", "Cookie-authenticated writes from other origins are refused") as r:
            own = f"{urlsplit(self.o.url).scheme}://{urlsplit(self.o.url).netloc}"
            cases = [
                ("no X-SlotServe header, no Origin", {}, 403),
                ("Origin: null", {"X-SlotServe": "1", "Origin": "null"}, 403),
                ("foreign Origin", {"X-SlotServe": "1", "Origin": "https://attacker.invalid"}, 403),
                ("Sec-Fetch-Site: cross-site", {"X-SlotServe": "1", "Sec-Fetch-Site": "cross-site"}, 403),
                ("Sec-Fetch-Site: same-site (another port)", {"X-SlotServe": "1", "Sec-Fetch-Site": "same-site"}, 403),
                ("own Origin", {"Origin": own}, 200),
                ("X-SlotServe: 1", {"X-SlotServe": "1"}, 200),
            ]
            wrong = []
            async with self.user.client() as c:
                c.cookies = self.user.http.cookies
                for label, headers, expected in cases:
                    resp = await c.put("/api/auth/preferences", json={}, headers=headers)
                    r.note(f"{label}: HTTP {resp.status_code} (expected {expected})")
                    if resp.status_code != expected:
                        wrong.append(f"{label} -> {resp.status_code}")
            if wrong:
                raise Fail("; ".join(wrong))
            r.detail = f"{len(cases)} cases as expected"

    def csrf_browser(self) -> None:
        path = self.o.workdir / f"csrf-poc-{self.run_id}.html"
        path.write_text(CSRF_POC.replace("{{BASE}}", self.o.url.rstrip("/")), encoding="utf-8")
        self.skipped(
            "security.csrf_browser",
            "Browser CSRF PoC (Chrome, Firefox)",
            f"open {path.name} (written next to the report) in Chrome and Firefox while signed in to SlotServe, "
            "from another origin (e.g. `python -m http.server 9999`, then also from a dev environment's direct "
            "port); afterwards no API key named csrf-poc may exist and the language must be unchanged",
            MANUAL,
        )

    # ---- GPU booking + LLM ----------------------------------------------------------------------------------------

    async def gpu_booking(self) -> None:
        async with self.check(
            "gpu.booking_llm", "Book a GPU lane, the model loads, an OpenAI-style request works"
        ) as r:
            models = [m for m in await self.user.get("/api/models") if m["status"] == "ready"]
            if self.o.model:
                model = next((m for m in models if m["name"] == self.o.model), None)
                if model is None:
                    raise Fail(f"no ready model {self.o.model!r}")
            else:
                default = self.lane.get("default_model_id")
                model = next((m for m in models if m["id"] == default), models[0] if models else None)
            if model is None:
                raise Skip("no ready model (add one, or pass --model)")
            booking = await self.book(30, model_id=model["id"])
            r.note(f"booked #{booking['id']} with {model['name']} on {self.lane['name']}")
            try:

                async def loaded():
                    status = await self.user.get("/api/status")
                    lane = next((x for x in status.get("lanes", []) if x["id"] == self.lane["id"]), None)
                    rt = (lane or status)["runtime"]
                    if rt["state"] == "failed":
                        raise Fail(f"the model failed to load: {rt.get('error')}")
                    return rt if rt["state"] == "ready" and rt["model_name"] == model["name"] else None

                started = time.monotonic()
                await wait_for(f"{model['name']} to load", loaded, self.o.load_timeout, self.o.poll)
                load_s = time.monotonic() - started
                key = await self.user.call("POST", "/api/keys", json={"name": f"verify-{self.run_id}"})
                self.key_ids.append(key["id"])
                async with self.user.client(headers={"Authorization": f"Bearer {key['key']}"}) as c:
                    listed = await c.get("/v1/models")
                    assert listed.status_code == 200, f"/v1/models: HTTP {listed.status_code}"
                    assert model["name"] in [m["id"] for m in listed.json().get("data", [])], "model not in /v1/models"
                    body = {
                        "model": model["name"],
                        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                        "max_tokens": 16,
                    }
                    resp = await c.post("/v1/chat/completions", json=body)
                assert resp.status_code == 200, f"/v1/chat/completions: HTTP {resp.status_code} {resp.text[:200]}"
                answer = resp.json()["choices"][0]["message"].get("content") or ""
                r.note(f"answer: {answer[:200]!r}")
                r.detail = f"{model['name']} ready after {load_s:.0f} s; chat completion answered"
            finally:
                await self.release(booking["id"])

    # ---- storage -------------------------------------------------------------------------------------------------

    async def storage(self) -> None:
        async with self.check("storage.check", "One storage: reachable and writable") as r:
            rows = await self.admin.get("/api/admin/storage")
            if self.o.storage_id is not None:
                st = next((s for s in rows if s["id"] == self.o.storage_id), None)
                if st is None:
                    raise Fail(f"no storage #{self.o.storage_id}")
            elif self.o.storage_host_path:
                st = await self.admin.call(
                    "POST",
                    "/api/admin/storage",
                    json={
                        "name": f"verify-{self.run_id}",
                        "kind": "host",
                        "purpose": "shared",
                        "host_path": self.o.storage_host_path,
                        "mount_path": f"/mnt/verify-{self.run_id}",
                    },
                )
                self.created_storage = st["id"]
            else:
                raise Skip("pass --storage-id (an existing storage) or --storage-host-path (creates a host folder)")
            checked = await self.admin.call("POST", f"/api/admin/storage/{st['id']}/check")
            status = checked.get("status") or {}
            r.note(f"{st['kind']} {st['source']}: {status}")
            if not status.get("ok"):
                raise Fail(f"{st['kind']} {st['source']}: {status.get('error')}")
            if status.get("writable") is False and not st.get("read_only"):
                raise Fail(f"{st['kind']} {st['source']}: not writable")
            if st["purpose"] == "shared" and st.get("access") == "everyone" and st.get("enabled"):
                self.storage_path = checked.get("container_path")
            r.detail = f"{st['kind']} {st['source']} ok (free {status.get('free')})"

    # ---- dev environments ----------------------------------------------------------------------------------------

    async def templates(self) -> None:
        ids = [f"devenv.template.{k}" for k in self.o.templates]
        if not self.devenv.get("enabled"):
            for i in ids:
                self.skipped(i, "Built-in template", "dev environments are switched off on this server")
            return
        templates = {t["builtin_key"]: t for t in await self.user.get("/api/devenv/templates") if t["builtin_key"]}
        for key in self.o.templates:
            async with self.check(f"devenv.template.{key}", f"Book the built-in template {key}") as r:
                t = templates.get(key)
                if t is None or not t["enabled"]:
                    raise Skip("not offered on this server (switched off by an admin)")
                await self.one_template(r, t)
        if not self.hardening_done:
            self.skipped("security.container_hardening", "Container hardening", "no environment could be checked")
        if not self.web_origin_done:
            self.skipped("security.web_origin", "Web origin isolation", "no web interface could be opened")
        self.skipped(
            "security.web_origin_browser",
            "Browser PoC: one environment's page reaching another's",
            "with two environments that have Jupyter (A and B, B signed in), open A's Jupyter in Chrome and "
            "Firefox and run in its console: fetch('/devenv/<B>/port/8888/api/contents', {referrerPolicy: "
            "'no-referrer'}).then(r => r.status) - it must be 403; a no-cors POST to B must not create a file",
            MANUAL,
        )

    async def one_template(self, r: Result, t: dict) -> None:
        booking = await self.book(self.o.booking_minutes, devenv_template_id=t["id"])
        r.note(f"booked #{booking['id']} ({t['image']})")
        try:
            started = time.monotonic()
            s = await self.session_for(r, lambda s: s.get("reservation_id") == booking["id"], self.o.devenv_timeout)
            parts = [f"running after {time.monotonic() - started:.0f} s"]
            if self.ssh.missing():
                parts.append(f"SSH not checked ({self.ssh.missing()})")
            else:
                parts += await self.ssh_checks(r, s, gpus=len(self.lane["gpus"]))
                if not self.hardening_done:
                    await self.hardening(s)
            if s["web"] and self.o.web:
                parts += await self.web_checks(r, s)
            if self.o.save_image and key_is_first(t, self.o.templates):
                await self.save_image(s)
            r.detail = "; ".join(parts)
        finally:
            await self.release(booking["id"])
            with contextlib.suppress(TimeoutError):

                async def gone():
                    rows = await self.user.get("/api/devenv/sessions")
                    return not any(x.get("reservation_id") == booking["id"] and x["state"] == "running" for x in rows)

                await wait_for("the environment to stop", gone, 180, self.o.poll)

    async def ssh_checks(self, r: Result, s: dict, gpus: int) -> list[str]:
        host, port, user = self.ssh_target(s)
        out = []
        if self.ssh.key is not None:
            login = await self.ssh.wait_login(host, port, user)
            if login.code != 0:
                raise Fail(f"SSH key login to {user}@{host}:{port} failed: {login.err.strip()[-300:]}")
            probe = (
                "echo SLOTSERVE_OK; nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true; test -w /workspace && echo W"
            )
            res = await self.ssh.run(host, port, user, probe)
            lines = res.out.split()
            r.note(f"ssh probe: {res.out.strip()!r} {res.err.strip()[-200:]!r}")
            assert lines[:1] == ["SLOTSERVE_OK"], f"unexpected SSH output {res.out!r}"
            seen = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else 0
            if gpus and seen != gpus:
                raise Fail(f"the container sees {seen} GPU(s), the lane has {gpus}")
            if "W" not in lines[2:]:
                raise Fail("/workspace is not writable")
            out.append(f"SSH key login ok, {seen} GPU(s) visible, /workspace writable")
        else:
            out.append("SSH key login not checked (no ssh-keygen)")
        if self.ssh.can_password:
            res = await self.ssh.run(host, port, user, "echo PW_OK", password=self.devenv_password)
            if "PW_OK" not in res.out:
                # password login may be switched off by the admin (devenv_password_login)
                r.note(f"password login: {res.code} {res.err.strip()[-200:]}")
                out.append("password login refused (switched off on this server?)")
            else:
                bad = await self.ssh.run(host, port, user, "true", password=self.devenv_password + "x")
                if bad.code == 0:
                    raise Fail("a wrong environment password was accepted")
                out.append("password login ok, wrong password refused")
        else:
            out.append("password login not checked (no sshpass)")
        return out

    async def web_checks(self, r: Result, s: dict) -> list[str]:
        out = []
        async with self.user.client(follow=True) as c:
            c.cookies = self.user.http.cookies
            for w in s["web"]:

                async def opened(w=w):
                    resp = await c.get(w["url"])
                    r.note(f"web {w['name']}: HTTP {resp.status_code} at {resp.url}")
                    return resp if resp.status_code < 500 else None

                resp = await wait_for(f"the web interface {w['name']}", opened, 180, max(self.o.poll, 3))
                if resp.status_code >= 400:
                    raise Fail(f"web interface {w['name']}: HTTP {resp.status_code}")
                out.append(f"web {w['name']} ok")
                if not self.web_origin_done:
                    await self.web_origin(c, s, resp)
        return out

    # ---- L4 security: container hardening (R1-05, W1-01) and web origins (R1-03, W1-02) -----------------------------

    async def has_preview(self) -> bool:
        """Whether the server has W1-01's container spec preview (and with it harden())."""
        resp = await self.user.http.post("/api/devenv/preview", json={})
        return not (resp.status_code == 405 or (resp.status_code == 404 and "Not Found" in resp.text))

    async def hardening(self, s: dict) -> None:
        self.hardening_done = True
        async with self.check("security.container_hardening", "Containers run hardened (R1-05)") as r:
            host, port, user = self.ssh_target(s)
            cmd = (
                "grep -E '^(NoNewPrivs|CapEff):' /proc/self/status; echo pids=$(cat /sys/fs/cgroup/pids.max); "
                "echo mem=$(cat /sys/fs/cgroup/memory.max)"
            )
            res = await self.ssh.run(host, port, user, cmd)
            r.note(res.out.strip())
            values = dict(re.findall(r"^(\w+)[:=]\s*(\S*)", res.out, re.M))
            problems = []
            if values.get("NoNewPrivs") != "1":
                problems.append("no-new-privileges is off")
            cap = int(values.get("CapEff", "0") or "0", 16)
            kept = [name for name, bit in DROPPED_CAPS.items() if cap & (1 << bit)]
            if kept:
                problems.append(f"capabilities not dropped: {', '.join(kept)}")
            if values.get("pids") in (None, "", "max"):
                problems.append("no PID limit")
            if values.get("mem") in (None, "", "max"):
                problems.append("no memory limit")
            if problems:
                if not await self.has_preview():
                    raise Pending(f"server predates container hardening (W1-01): {'; '.join(problems)}")
                raise Fail("; ".join(problems))
            r.detail = f"no-new-privileges, capabilities dropped, pids.max {values['pids']}, memory.max {values['mem']}"

    async def web_origin(self, c: httpx.AsyncClient, s: dict, opened: httpx.Response) -> None:
        self.web_origin_done = True
        async with self.check("security.web_origin", "A page of one environment cannot use another's (R1-03)") as r:
            url = opened.url
            if (url.host, url.port) == (urlsplit(self.o.url).hostname, urlsplit(self.o.url).port):
                raise Skip("web interfaces are served on SlotServe's own port (devenv_web_port = 0)")
            origin = f"{url.scheme}://{url.netloc.decode()}"
            target = str(url)
            nav = await c.get(target, headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})
            other = f"{origin}/devenv/{s['id'] + 100000}/port/8888/"
            fetch = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
            from_other = await c.get(target, headers={**fetch, "Referer": other})
            no_referer = await c.get(target, headers=fetch)
            policy = nav.headers.get("referrer-policy", "")
            r.note(f"navigation {nav.status_code} (Referrer-Policy {policy or 'none'})")
            r.note(f"fetch with another environment's Referer {from_other.status_code}")
            r.note(f"fetch without Referer {no_referer.status_code}")
            if nav.status_code >= 400:
                raise Fail(f"opening the page itself failed: HTTP {nav.status_code}")
            if from_other.status_code != 403:
                raise Fail(f"a request from another environment's page was allowed (HTTP {from_other.status_code})")
            if no_referer.status_code != 403:
                if policy != "same-origin":
                    raise Pending("server predates fail-closed web origins (W1-02): a request without Referer passes")
                raise Fail(f"a same-origin request without Referer was allowed (HTTP {no_referer.status_code})")
            r.detail = "navigation ok; requests from another environment or without Referer refused"

    async def save_image(self, s: dict) -> None:
        async with self.check("devenv.save_image", "Save a running environment as an image") as r:
            name = f"verify-{self.run_id}"
            await self.user.call("POST", f"/api/devenv/sessions/{s['id']}/save", json={"name": name})

            async def saved():
                rows = await self.user.get("/api/devenv/templates")
                return next((t for t in rows if t["kind"] == "saved" and t["name"] == name), None)

            t = await wait_for("the saved image", saved, 1800, self.o.poll)
            r.note(f"saved as {t['image']}")
            await self.user.call("DELETE", f"/api/devenv/templates/{t['id']}", expect=(204, 404))
            r.detail = f"saved as {t['image']} (template removed again)"

    # ---- CPU-only ------------------------------------------------------------------------------------------------

    async def cpu_only(self) -> None:
        title = "A dev environment without a GPU (cores, memory limit)"
        if not self.devenv.get("enabled"):
            self.skipped("devenv.cpu_only", title, "dev environments are switched off on this server")
            self.skipped("storage.mount", "Storage inside an environment", "dev environments are switched off")
            return
        session: dict | None = None
        async with self.check("devenv.cpu_only", title) as r:
            info = await self.user.get("/api/devenv/cpu")
            if not info["enabled"]:
                raise Skip("CPU-only environments are switched off on this server")
            templates = {t["builtin_key"]: t for t in await self.user.get("/api/devenv/templates") if t["builtin_key"]}
            t = templates.get(self.o.cpu_template)
            if t is None:
                raise Skip(f"no built-in template {self.o.cpu_template!r}")
            started = await self.user.call("POST", "/api/devenv/cpu/start", json={"template_id": t["id"]})
            self.cpu_sessions.append(started["id"])
            session = await self.session_for(r, lambda s: s["id"] == started["id"], self.o.devenv_timeout)
            parts = ["running"]
            if self.ssh.missing() or self.ssh.key is None:
                parts.append("limits not checked (no SSH)")
            else:
                host, port, user = self.ssh_target(session)
                login = await self.ssh.wait_login(host, port, user)
                if login.code != 0:
                    raise Fail(f"SSH login failed: {login.err.strip()[-300:]}")
                cmd = "nproc; cat /sys/fs/cgroup/memory.max; nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true"
                res = await self.ssh.run(host, port, user, cmd)
                r.note(f"probe: {res.out.strip()!r}")
                lines = res.out.split()
                assert len(lines) >= 3, f"unexpected output {res.out!r}"
                if info.get("pool_cpus") and int(lines[0]) != info["pool_cpus"]:
                    raise Fail(f"nproc is {lines[0]}, the CPU pool has {info['pool_cpus']} cores ({info['pool']})")
                if info.get("memory_gb") and lines[1] != str(int(info["memory_gb"] * GiB)):
                    raise Fail(f"memory.max is {lines[1]}, the setting is {info['memory_gb']} GB")
                if lines[2] != "0":
                    raise Fail(f"{lines[2]} GPU(s) visible in a CPU-only environment")
                parts.append(f"nproc {lines[0]}, memory.max {lines[1]}, no GPU")
            r.detail = "; ".join(parts)
        try:
            await self.storage_mount(session)
            await self.forkbomb(session)
        finally:
            for sid in list(self.cpu_sessions):
                with contextlib.suppress(ApiFailure):
                    await self.user.call("POST", f"/api/devenv/cpu/sessions/{sid}/stop", expect=(202, 404, 409))
                self.cpu_sessions.remove(sid)

    async def storage_mount(self, session: dict | None) -> None:
        title = "The storage is mounted and writable inside an environment"
        if session is None or self.storage_path is None or self.ssh.key is None:
            why = "no running CPU-only environment" if session is None else "no shared storage for everyone"
            self.skipped("storage.mount", title, why if self.ssh.key is not None or session is None else "no SSH")
            return
        async with self.check("storage.mount", title) as r:
            host, port, user = self.ssh_target(session)
            f = f"{self.storage_path}/.slotserve-verify-{self.run_id}"
            res = await self.ssh.run(host, port, user, f"echo ok > {f} && cat {f} && rm -f {f}")
            r.note(f"{res.code} {res.out.strip()} {res.err.strip()}")
            if "ok" not in res.out:
                raise Fail(f"could not write to {self.storage_path}: {res.err.strip()[-200:]}")
            r.detail = f"wrote and removed a file in {self.storage_path}"

    async def forkbomb(self, session: dict | None) -> None:
        title = "A fork bomb in an environment does not take the server down"
        if not self.o.destructive:
            self.skipped("security.forkbomb", title, "destructive checks are off (--destructive)")
            return
        if session is None or self.ssh.key is None:
            self.skipped("security.forkbomb", title, "no CPU-only environment reachable over SSH")
            return
        async with self.check("security.forkbomb", title) as r:
            host, port, user = self.ssh_target(session)
            bomb = asyncio.create_task(
                self.ssh.run(
                    host, port, user, "timeout 30 sh -c 'b(){ b|b& }; b' >/dev/null 2>&1; echo done", timeout=90
                )
            )
            failures = 0
            async with self.admin.client() as c:
                for _ in range(40):
                    try:
                        ok = (await c.get("/healthz", timeout=5)).status_code == 200
                    except httpx.HTTPError:
                        ok = False
                    failures += not ok
                    await asyncio.sleep(1)
            await bomb
            r.note(f"{failures} of 40 health probes failed")
            if failures:
                raise Fail(f"/healthz failed {failures} times during the fork bomb")
            r.detail = "/healthz answered 40 of 40 probes"

    # ---- clean-up --------------------------------------------------------------------------------------------------

    async def cleanup(self) -> None:
        if self.user is None:
            return
        problems = []
        for b in list(self.bookings):
            await self.release(b)
        for k in self.key_ids:
            with contextlib.suppress(ApiFailure):
                await self.user.call("DELETE", f"/api/keys/{k}", expect=(204, 404))
        if self.o.keep:
            self.skipped("cleanup", "Clean up", f"kept (--keep): account verify-{self.run_id}")
            return
        if self.created_storage is not None:
            try:
                await self.admin.call("DELETE", f"/api/admin/storage/{self.created_storage}", expect=(204, 404))
            except ApiFailure as exc:
                problems.append(str(exc))
        if self.user_row is not None:
            try:
                await self.admin.call("DELETE", f"/api/admin/users/{self.user_row['id']}", expect=(204, 404))
            except ApiFailure as exc:
                problems.append(str(exc))
        if problems:
            self.skipped("cleanup", "Clean up", "; ".join(problems), FAIL)


def key_is_first(t: dict, keys: tuple[str, ...]) -> bool:
    return bool(keys) and t.get("builtin_key") == keys[0]


CSRF_POC = """<!doctype html>
<meta charset="utf-8">
<title>SlotServe CSRF PoC (tools/verify)</title>
<h1>SlotServe CSRF PoC</h1>
<p>Open this page from a different origin while signed in to <code>{{BASE}}</code>. Every attempt below must fail:
afterwards there must be no API key named <code>csrf-poc</code> and the interface language must be unchanged.</p>
<ol id="log"></ol>
<form id="f" method="POST" action="{{BASE}}/api/keys" enctype="text/plain" target="sink">
  <input type="hidden" name='{"name":"csrf-poc","x":"' value='"}'>
</form>
<iframe name="sink" style="display:none"></iframe>
<script>
const base = "{{BASE}}";
const log = (m) => {
  const li = document.createElement("li");
  li.textContent = m;
  document.getElementById("log").append(li);
};
async function attempt(label, url, init) {
  try { const r = await fetch(url, init); log(label + ": sent (status hidden: " + r.type + ")"); }
  catch (e) { log(label + ": blocked by the browser (" + e + ")"); }
}
(async () => {
  await attempt("no-cors POST /api/keys", base + "/api/keys",
    {method: "POST", mode: "no-cors", credentials: "include", headers: {"Content-Type": "text/plain"},
     body: JSON.stringify({name: "csrf-poc"})});
  await attempt("no-cors PUT /api/auth/preferences", base + "/api/auth/preferences",
    {method: "PUT", mode: "no-cors", credentials: "include", body: JSON.stringify({locale: "zh-CN"})});
  await attempt("cors POST with X-SlotServe (needs a preflight SlotServe never grants)", base + "/api/keys",
    {method: "POST", credentials: "include", headers: {"X-SlotServe": "1", "Content-Type": "application/json"},
     body: JSON.stringify({name: "csrf-poc"})});
  document.getElementById("f").submit();
  log("form POST (text/plain) submitted");
})();
</script>
"""

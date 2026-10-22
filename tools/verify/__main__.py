"""Command line: ``python -m tools.verify --url https://gpu-box:8000 --admin admin`` (from the repository root).

The admin password is read from ``--admin-password``, else ``SLOTSERVE_VERIFY_PASSWORD``, else asked for.
Writes ``verify-report-<host>-<version>.md`` and ``.json`` (and the CSRF PoC page) to ``--out``; exits 1 when a
check failed, 2 when the server could not be reached or signed in to.
"""

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from .checks import BUILTIN_TEMPLATES, Options, Verifier
from .report import FAIL


def parse(argv: list[str] | None = None) -> Options:
    p = argparse.ArgumentParser(
        prog="python -m tools.verify",
        description="Verify a running SlotServe on real hardware (L4) and write a report for the release PR.",
    )
    p.add_argument("--url", required=True, help="SlotServe's address, e.g. http://gpu-box:8000")
    p.add_argument("--admin", required=True, help="an admin account")
    p.add_argument("--admin-password", default=os.environ.get("SLOTSERVE_VERIFY_PASSWORD", ""))
    p.add_argument("--lane", default="", help="lane id or name to book (default: the first lane with GPUs)")
    p.add_argument("--model", default="", help="model to load (default: the lane's default, else the first ready)")
    p.add_argument(
        "--templates",
        default=",".join(BUILTIN_TEMPLATES),
        help="built-in templates to start, comma separated (default: all six)",
    )
    p.add_argument("--cpu-template", default="pytorch", help="template of the CPU-only environment")
    p.add_argument("--storage-id", type=int, default=None, help="check this existing storage")
    p.add_argument("--storage-host-path", default="", help="create a host-folder storage at this path and check it")
    p.add_argument("--ssh-host", default="", help="host for SSH logins (default: what SlotServe shows)")
    p.add_argument("--no-ssh", action="store_true", help="do not log in to environments")
    p.add_argument("--no-web", action="store_true", help="do not open environments' web interfaces")
    p.add_argument("--save-image", action="store_true", help="also save the first environment as an image")
    p.add_argument("--destructive", action="store_true", help="also run a fork bomb in a CPU-only environment")
    p.add_argument("--keep", action="store_true", help="keep the test account (for debugging)")
    p.add_argument("--load-timeout", type=float, default=900, help="seconds for a model to load")
    p.add_argument("--devenv-timeout", type=float, default=1800, help="seconds for an environment (incl. pull)")
    p.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    p.add_argument("--out", default=".", help="directory for the report")
    a = p.parse_args(argv)
    password = a.admin_password or getpass.getpass(f"Password of {a.admin}: ")
    templates = tuple(t.strip() for t in a.templates.split(",") if t.strip())
    return Options(
        url=a.url,
        admin_user=a.admin,
        admin_password=password,
        lane=a.lane,
        model=a.model,
        templates=templates,
        cpu_template=a.cpu_template,
        storage_id=a.storage_id,
        storage_host_path=a.storage_host_path,
        ssh_host=a.ssh_host,
        ssh=not a.no_ssh,
        web=not a.no_web,
        save_image=a.save_image,
        destructive=a.destructive,
        keep=a.keep,
        load_timeout=a.load_timeout,
        devenv_timeout=a.devenv_timeout,
        verify_tls=not a.insecure,
        workdir=Path(a.out),
    )


def main(argv: list[str] | None = None) -> int:
    opts = parse(argv)
    opts.workdir.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(Verifier(opts).run())
    md, js = report.write(opts.workdir)
    c = report.counts()
    print(f"\n{c['pass']} pass, {c['fail']} fail, {c['skip']} skipped, {c['pending']} pending, {c['manual']} manual")
    print(f"report: {md}\n        {js}")
    if report.results and report.results[0].id == "server" and report.results[0].status == FAIL:
        return 2
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

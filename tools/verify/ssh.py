"""Logging in to dev environments with the system's OpenSSH client (and sshpass for passwords)."""

import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "LogLevel=ERROR",
]


@dataclass
class SshResult:
    code: int
    out: str
    err: str


class Ssh:
    def __init__(self, workdir: Path, enabled: bool = True):
        self.workdir = workdir
        self.enabled = enabled and bool(shutil.which("ssh"))
        self.can_keygen = self.enabled and bool(shutil.which("ssh-keygen"))
        self.can_password = self.enabled and bool(shutil.which("sshpass"))
        self.key: Path | None = None

    def missing(self) -> str:
        if not self.enabled:
            return "no ssh client (or --no-ssh)"
        return ""

    async def make_key(self) -> str | None:
        """A fresh ed25519 key pair for this run; returns the public key."""
        if not self.can_keygen:
            return None
        self.key = self.workdir / "verify_ed25519"
        for p in (self.key, self.key.with_suffix(".pub")):
            p.unlink(missing_ok=True)
        cmd = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "slotserve-verify", "-f", str(self.key)]
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
        return self.key.with_suffix(".pub").read_text().strip()

    async def run(
        self, host: str, port: int, user: str, command: str, password: str | None = None, timeout: float = 120
    ) -> SshResult:
        cmd = ["ssh", *OPTS, "-p", str(port)]
        if password is not None:
            cmd = ["sshpass", "-p", password, *cmd, "-o", "PubkeyAuthentication=no", "-o", "BatchMode=no"]
        else:
            cmd += ["-o", "BatchMode=yes", "-o", "PasswordAuthentication=no", "-i", str(self.key)]
        cmd += [f"{user}@{host}", command]
        try:
            p = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return SshResult(124, "", f"timed out after {timeout:.0f} s")
        return SshResult(p.returncode, p.stdout, p.stderr)

    async def wait_login(self, host: str, port: int, user: str, timeout: float = 90) -> SshResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            r = await self.run(host, port, user, "true", timeout=20)
            if r.code == 0 or loop.time() > deadline:
                return r
            await asyncio.sleep(2)

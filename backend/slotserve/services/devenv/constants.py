"""Constants of dev environments, the error they raise and the names of their Docker objects."""

import re

from ...containers.names import LABEL, MAX_AUTH_TRIES, UID_BASE, WEB_KINDS, network_name, project_network_name

TICK_SECONDS = 3.0
ACCOUNTS_REFRESH_SECONDS = 60.0
HOUSEKEEPING_SECONDS = 3600.0
OPEN_STATES = ("waiting", "pulling", "ready", "starting", "running", "stopping", "failed")
GPU_STATES = ("starting", "running", "stopping")
KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:@]{0,250}$", re.I)
PATH_RE = re.compile(r"^/[A-Za-z0-9._+@/-]{1,200}$")
MAX_PERSIST_PATHS = 8
# Paths that cannot be kept: system directories, SlotServe's own and the ones that are mounted anyway.
FORBIDDEN_PATHS = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/run",
    "/etc",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/usr",
    "/boot",
    "/tmp",
    "/opt/slotserve",
    "/workspace",
    "/models",
)
# Names a SlotServe user cannot get as a Unix account (system accounts and the usual image users).
RESERVED_NAMES = {
    "root",
    "daemon",
    "bin",
    "sys",
    "sync",
    "games",
    "man",
    "lp",
    "mail",
    "news",
    "uucp",
    "proxy",
    "www-data",
    "backup",
    "list",
    "irc",
    "gnats",
    "nobody",
    "systemd-network",
    "systemd-resolve",
    "messagebus",
    "sshd",
    "ubuntu",
    "jovyan",
    "admin",
    "operator",
    "halt",
    "shutdown",
    "ftp",
    "postgres",
    "mysql",
    "redis",
    "_apt",
}


class DevEnvError(Exception):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


__all__ = [
    "ACCOUNTS_REFRESH_SECONDS",
    "FORBIDDEN_PATHS",
    "GPU_STATES",
    "HOUSEKEEPING_SECONDS",
    "IMAGE_RE",
    "KEY_TYPES",
    "LABEL",
    "MAX_AUTH_TRIES",
    "MAX_PERSIST_PATHS",
    "OPEN_STATES",
    "PATH_RE",
    "RESERVED_NAMES",
    "TICK_SECONDS",
    "UID_BASE",
    "WEB_KINDS",
    "DevEnvError",
    "network_name",
    "project_network_name",
]

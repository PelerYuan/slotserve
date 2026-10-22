"""Constants of dev environments, the error they raise and the names of their Docker objects."""

import re

TICK_SECONDS = 3.0
ACCOUNTS_REFRESH_SECONDS = 60.0
HOUSEKEEPING_SECONDS = 3600.0
OPEN_STATES = ("waiting", "pulling", "ready", "starting", "running", "stopping", "failed")
GPU_STATES = ("starting", "running", "stopping")
LABEL = "slotserve.devenv"
UID_BASE = 20000  # Unix uid of a SlotServe user in shared environments: UID_BASE + user id
MAX_AUTH_TRIES = 3
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
WEB_KINDS = ("desktop", "jupyter", "vscode", "http")
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


def network_name(session_id: int) -> str:
    return f"slotserve-devenv-{session_id}"


def project_network_name(project_id: int) -> str:
    """A kept environment's container keeps its network between bookings (until the project is deleted)."""
    return f"slotserve-devenv-p{project_id}"

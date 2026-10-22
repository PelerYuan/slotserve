"""Dev environments: Docker containers with SSH, for a booking or for whoever wants an idle lane.

When a dev environment booking comes up, its lane's model server is stopped (it is scheduled like a whole-GPU
booking), the image is pulled ahead of time, and a container is started with the lane's GPUs, the person's
persistent ``/workspace`` and the shared models read-only. SSH comes from a static Dropbear that SlotServe
copies into the container, so any image works; logins use the person's public keys and, when admins allow
it, their environment password. When the booking ends the container is removed (``/workspace`` stays) - or,
for a kept environment ("project"), stopped and started again at the next booking.

A lane whose idle mode is ``devenv`` runs one shared container while nobody has it booked. Every SlotServe
user gets an account there with their own keys and password; it is stopped ahead of the next booking.

Every container gets its own Docker network (a kept environment keeps one for as long as it exists), and SlotServe
reverse-proxies any of the container's ports under ``/devenv/<session>/port/<port>/`` to the people allowed to use
the environment (see ``gateway/devenv_web.py``). A template declares the ports of its web interfaces (a desktop in
the browser, JupyterLab, VS Code or any HTTP app) with a name and kind. SlotServe reaches a port over the
environment's network when it runs in a container itself (it joins that network), otherwise through a random port
bound to 127.0.0.1 (declared ports) or the container's address on its network. Web ports are never published.
"""

# The package holds what was once services/devenv.py; every name it had is still importable from here.
from .constants import (
    ACCOUNTS_REFRESH_SECONDS,
    FORBIDDEN_PATHS,
    GPU_STATES,
    HOUSEKEEPING_SECONDS,
    IMAGE_RE,
    KEY_TYPES,
    LABEL,
    MAX_AUTH_TRIES,
    MAX_PERSIST_PATHS,
    OPEN_STATES,
    PATH_RE,
    RESERVED_NAMES,
    TICK_SECONDS,
    UID_BASE,
    WEB_KINDS,
    DevEnvError,
    network_name,
    project_network_name,
)
from .manager import (
    DevEnvManager,
    Plan,
    _json,
    _NoCookies,
    _port_taken,
    log,
)
from .scripts import (
    ACCOUNTS_SCRIPT,
    LIB_SCRIPT,
    PROFILE,
    ROOT_SETUP,
    SHARED_SETUP,
    SSH_SCRIPT,
    START_SCRIPT,
)
from .ssh import (
    SshBundle,
    booking_ssh_files,
    parse_public_key,
)
from .templates import (
    BUILTINS,
    WEB_FIELDS,
    fill_placeholders,
    load_web_ports,
    placeholder_values,
    port_entry,
    seed_templates,
    session_web,
    visible,
    web_base,
)
from .validation import (
    _slug,
    can_access,
    check_image,
    check_persist_paths,
    dump_paths,
    load_paths,
    parse_port_range,
    unix_name,
    unix_names,
)

__all__ = [
    "ACCOUNTS_REFRESH_SECONDS",
    "ACCOUNTS_SCRIPT",
    "BUILTINS",
    "FORBIDDEN_PATHS",
    "GPU_STATES",
    "HOUSEKEEPING_SECONDS",
    "IMAGE_RE",
    "KEY_TYPES",
    "LABEL",
    "LIB_SCRIPT",
    "MAX_AUTH_TRIES",
    "MAX_PERSIST_PATHS",
    "OPEN_STATES",
    "PATH_RE",
    "PROFILE",
    "RESERVED_NAMES",
    "ROOT_SETUP",
    "SHARED_SETUP",
    "SSH_SCRIPT",
    "START_SCRIPT",
    "TICK_SECONDS",
    "UID_BASE",
    "WEB_FIELDS",
    "WEB_KINDS",
    "DevEnvError",
    "DevEnvManager",
    "Plan",
    "SshBundle",
    "_NoCookies",
    "_json",
    "_port_taken",
    "_slug",
    "booking_ssh_files",
    "can_access",
    "check_image",
    "check_persist_paths",
    "dump_paths",
    "fill_placeholders",
    "load_paths",
    "load_web_ports",
    "log",
    "network_name",
    "parse_port_range",
    "parse_public_key",
    "placeholder_values",
    "port_entry",
    "project_network_name",
    "seed_templates",
    "session_web",
    "unix_name",
    "unix_names",
    "visible",
    "web_base",
]

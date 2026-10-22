"""Golden tests of what a dev environment container is created with (A01 step 0: the safety net for the move to a
container spec package).

Each case builds a small database in memory, runs ``DevEnvManager._plan`` and compares everything the plan holds -
the Docker create body, the files copied into the container, the folders and volumes to prepare, the network and the
web interfaces - with ``golden/<case>.json``. No Docker (not even the fake one) is involved: the CPU layout reads a
stub ``docker info`` and the models mount, SSH bundle and web token are fixed.

A change to a golden file is a change of what users' containers look like: review it like one. To rewrite the files
after an intended change run ``SLOTSERVE_UPDATE_GOLDEN=1 pytest tests/unit/containers``.
"""

import base64
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from slotserve.config import Settings
from slotserve.containers.harden import check
from slotserve.db import Base
from slotserve.orm import DevProject, DevSession, DevTemplate, Lane, Reservation, SshKey, Storage, User
from slotserve.services.devenv import BUILTINS, DevEnvError, DevEnvManager, SshBundle

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("SLOTSERVE_UPDATE_GOLDEN") == "1"
TOKEN = "golden-web-token"
START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
END = datetime(2026, 1, 5, 17, 30, tzinfo=UTC)
KEY_ALICE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGoldenAliceKeyGoldenAliceKeyGoldenAlice alice@laptop"
KEY_ALICE_2 = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQGoldenAliceRsa alice@desktop"
KEY_BOB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGoldenBobKeyGoldenBobKeyGoldenBobKeyGo bob@box"
HASH_ALICE = "$6$goldensalt$alicehash"
HASH_BOB = "$6$goldensalt$bobhash"


class StubDocker:
    """Only what ``_plan`` reaches: ``docker info`` for the CPU layout."""

    async def info(self) -> dict:
        return {"NCPU": 32, "MemTotal": 128 * 1024**3}


def builtin(key: str, **extra) -> DevTemplate:
    spec = next(b for b in BUILTINS if b["builtin_key"] == key)
    return DevTemplate(kind="builtin", **spec, **extra)


def settings(**overrides) -> Settings:
    values = {
        "secret_key": "golden-secret",
        "data_dir": Path("golden-data"),
        "models_dir": Path("golden-models"),
        "devenv_enabled": True,
        "devenv_password_login": True,
        "devenv_storage_dir": "",
        "devenv_shm_gb": 8.0,
        "devenv_cpus": 0.0,
        "devenv_memory_gb": 0.0,
        "devenv_cpu_enabled": True,
        "devenv_cpu_cores_per_gpu": 8,
        "devenv_cpu_cores": 4.0,
        "devenv_cpu_memory_gb": 16.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def people() -> list:
    """alice (key + password), bob (key + password, admin), carol (no key, no password), dave (disabled)."""
    return [
        User(id=1, username="alice", password_hash="x", devenv_password_hash=HASH_ALICE),
        User(id=2, username="Bob", password_hash="x", is_admin=True, devenv_password_hash=HASH_BOB),
        User(id=3, username="carol", password_hash="x"),
        User(id=4, username="dave", password_hash="x", disabled_at=START, devenv_password_hash="$6$s$dave"),
        SshKey(id=1, user_id=1, name="laptop", public_key=KEY_ALICE, fingerprint="SHA256:a"),
        SshKey(id=2, user_id=1, name="desktop", public_key=KEY_ALICE_2, fingerprint="SHA256:b"),
        SshKey(id=3, user_id=2, name="box", public_key=KEY_BOB, fingerprint="SHA256:c"),
        SshKey(id=4, user_id=4, name="old", public_key=KEY_BOB, fingerprint="SHA256:d"),
        Lane(id=1, name="Lane A", gpu_indices="0,1", position=0),
        Lane(id=2, name="Lane B", gpu_indices="2", position=1),
        Lane(id=3, name="any GPU", gpu_indices="", position=2),
    ]


def booking(session_id: int = 11, lane_id: int = 1, template_id: int | None = 1, **extra) -> list:
    return [
        Reservation(id=session_id, user_id=1, lane_id=lane_id, start_at=START, end_at=END, status="booked"),
        DevSession(
            id=session_id,
            kind="booking",
            reservation_id=session_id,
            user_id=1,
            template_id=template_id,
            lane_id=lane_id,
            image="registry.example/team/image:1",
            state="starting",
            **extra,
        ),
    ]


def host_storage(id: int, purpose: str, **extra) -> Storage:
    return Storage(
        id=id, name=f"{purpose}-{id}", kind="host", purpose=purpose, host_path=f"/mnt/{purpose}{id}", **extra
    )


def nfs_storage(id: int, purpose: str, **extra) -> Storage:
    return Storage(
        id=id, name=f"{purpose}-{id}", kind="nfs", purpose=purpose, server="nas.example", remote_path="/export", **extra
    )


# Each case: (rows, settings overrides, manager overrides); session 11 is planned. New rows for every call.
def cases() -> dict[str, tuple]:
    return {
        # A GPU booking from a plain template, the person's keys and password; legacy Docker volumes, models mounted.
        "booking_gpu_keys_and_password": ([builtin("pytorch", id=1), *booking()], {}, {}),
        # Password login switched off by admins: keys only ("-s"), root gets no password.
        "booking_keys_only_password_login_off": (
            [builtin("pytorch", id=1), *booking()],
            {"devenv_password_login": False},
            {},
        ),
        # Legacy storage directory (bind mounts to prepare), kept paths of the template, CPU and memory limits, no
        # models mount, on a lane with one GPU.
        "booking_storage_dir_template_paths_limits": (
            [
                DevTemplate(
                    id=5,
                    name="Custom",
                    image="registry.example/team/image:1",
                    kind="custom",
                    owner_id=1,
                    persist_paths=json.dumps(["/root/.cache", "/opt/conda/envs"]),
                ),
                *booking(lane_id=2, template_id=5),
            ],
            {"devenv_storage_dir": "/srv/slotserve/", "devenv_cpus": 6.5, "devenv_memory_gb": 48.0},
            {"models": ""},
        ),
        # A CPU-only environment: no GPU device request, the shared CPU pool and the CPU-only limits.
        "cpu_only": (
            [
                builtin("pytorch", id=1),
                DevSession(
                    id=11, kind="cpu", user_id=1, template_id=1, image="pytorch/pytorch:latest", state="starting"
                ),
            ],
            {},
            {},
        ),
        # A lane's shared idle environment: an account for every active person, their keys and password hashes.
        "shared_idle_accounts": (
            [
                builtin("pytorch", id=1),
                DevSession(
                    id=11, kind="shared", lane_id=2, template_id=1, image="pytorch/pytorch:latest", state="starting"
                ),
            ],
            {},
            {},
        ),
        # The same with password login off and managed storage: everyone's folder of the personal storage under
        # /workspaces, a shared read-only storage open to everyone and one only for some people (not mounted).
        "shared_idle_storage_password_login_off": (
            [
                builtin("pytorch", id=1),
                host_storage(1, "personal"),
                host_storage(2, "shared", mount_path="/data", read_only=True),
                host_storage(3, "shared", mount_path="/team", access="users", user_ids="[1]"),
                DevSession(
                    id=11, kind="shared", lane_id=2, template_id=1, image="pytorch/pytorch:latest", state="starting"
                ),
            ],
            {"devenv_password_login": False},
            {},
        ),
        # A kept environment ("project") resumed by a new booking: its own name, network and kept directories as
        # legacy volumes.
        "kept_project_resume": (
            [
                builtin("pytorch", id=1),
                DevProject(
                    id=7,
                    owner_id=1,
                    name="thesis",
                    template_id=1,
                    image="slotserve-saved/alice-thesis:3",
                    persist_paths=json.dumps(["/root/.cache"]),
                    state="paused",
                    container_id="abc123",
                    container_port=2205,
                ),
                *booking(project_id=7),
            ],
            {},
            {},
        ),
        # A kept environment on managed storage: personal storage for /workspace, an NFS projects storage for its kept
        # directories, a shared storage and a model library for the person, an admins-only storage (not mounted).
        "kept_project_managed_storage": (
            [
                builtin("pytorch", id=1),
                host_storage(1, "personal"),
                nfs_storage(2, "projects", options="nfsvers=4.1,soft"),
                host_storage(3, "shared", mount_path="/data", read_only=True),
                host_storage(4, "shared", mount_path="/team", access="users", user_ids="[1, 2]"),
                nfs_storage(5, "models", mount_path="/library", read_only=True, local_path="/mnt/library"),
                host_storage(6, "shared", mount_path="/admin", access="admins"),
                DevProject(
                    id=7,
                    owner_id=1,
                    name="thesis",
                    template_id=1,
                    image="slotserve-saved/alice-thesis:3",
                    persist_paths=json.dumps(["/root/.cache", "/home/alice/.local"]),
                ),
                *booking(project_id=7),
            ],
            {},
            {},
        ),
        # Managed personal and projects storage for a template's kept paths (no project).
        "booking_managed_storage_template_paths": (
            [
                DevTemplate(
                    id=5,
                    name="Custom",
                    image="registry.example/team/image:1",
                    kind="custom",
                    owner_id=1,
                    persist_paths=json.dumps(["/root/.cache"]),
                ),
                nfs_storage(1, "personal"),
                host_storage(2, "projects"),
                *booking(template_id=5),
            ],
            {},
            {},
        ),
        # Web UI templates, SlotServe on the host: declared ports bound to 127.0.0.1, token and base path filled in.
        "web_jupyter_on_host": ([builtin("jupyter-pytorch", id=2), *booking(template_id=2)], {}, {}),
        "web_desktop_keep_entrypoint": ([builtin("desktop", id=3), *booking(template_id=3)], {}, {}),
        # SlotServe in a container: no port bindings (it joins the environment's network).
        "web_vscode_in_container": ([builtin("vscode", id=4), *booking(template_id=4)], {}, {"in_container": True}),
        # A custom template with several ports (a direct one, one that knows its base path, a plain HTTP app), its own
        # environment with placeholders and a command.
        "web_custom_ports_env": (
            [
                DevTemplate(
                    id=6,
                    name="Apps",
                    image="registry.example/apps:2",
                    kind="custom",
                    owner_id=1,
                    command="serve --root {base_path_9000} --token {token} --session {session} {unknown}",
                    env=json.dumps({"APP_BASE": "{base_path}", "APP_PORT": "{port}", "PLAIN": "x"}),
                    web_ports=json.dumps(
                        [
                            {"name": "Main", "port": 9000, "kind": "http"},
                            {"name": "Board", "port": 6006, "kind": "unknown-kind"},
                            {"name": "API", "port": 7000, "kind": "http", "mode": "direct"},
                            {"name": "dup", "port": 9000},
                            {"name": "bad", "port": 70000},
                        ]
                    ),
                ),
                *booking(template_id=6),
            ],
            {},
            {},
        ),
    }


def _file(content: bytes, mode: int) -> dict:
    try:
        return {"mode": oct(mode), "text": content.decode()}
    except UnicodeDecodeError:
        return {"mode": oct(mode), "base64": base64.b64encode(content).decode()}


def _normalize(plan) -> dict:
    sp = plan.storage
    return {
        "name": plan.name,
        "config": plan.config,
        "files": {path: _file(*plan.files[path]) for path in sorted(plan.files)},
        "binds": [list(b) for b in plan.binds],
        "accounts_digest": plan.accounts_digest,
        "network": plan.network,
        "network_labels": plan.network_labels,
        "web": plan.web,
        "keep_entrypoint": plan.keep_entrypoint,
        "storage": None
        if sp is None
        else {
            "personal": sp.personal.id if sp.personal is not None else None,
            "projects": sp.projects.id if sp.projects is not None else None,
            "mounts": sp.mounts,
            "extras": sp.extras,
            "volumes": {name: list(value) for name, value in sorted(sp.volumes.items())},
            "preps": {str(k): [vars(p) for p in v] for k, v in sorted(sp.preps.items())},
            "signature": sp.signature,
        },
    }


class SyncDb:
    """The two calls ``_plan`` makes on its database session, over a synchronous one (no event loop thread)."""

    def __init__(self, session: Session):
        self.session = session

    async def get(self, entity, ident):
        return self.session.get(entity, ident)

    async def execute(self, statement):
        return self.session.execute(statement)


ENGINE = create_engine("sqlite://", poolclass=StaticPool)
Base.metadata.create_all(ENGINE)


async def _plan(rows: list, settings_overrides: dict, manager_overrides: dict) -> dict:
    with Session(ENGINE, expire_on_commit=False) as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        for row in [*people(), *rows]:
            db.add(row)
            db.flush()  # in order: rows refer to the ones before them
        db.commit()
    manager = DevEnvManager(SimpleNamespace(settings=settings(**settings_overrides)), docker=StubDocker())
    manager.in_container = manager_overrides.get("in_container", False)
    manager._models_source = manager_overrides.get("models", "/srv/models")
    manager._bundle = SshBundle(b"ELF golden dropbear", b"golden host key", "SHA256:golden")
    with Session(ENGINE, expire_on_commit=False) as db:
        return _normalize(await manager._plan(SyncDb(db), db.get(DevSession, 11)))


@pytest.mark.parametrize("case", sorted(cases()))
async def test_plan_golden(case, monkeypatch):
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n=None: TOKEN)
    rows, settings_overrides, manager_overrides = cases()[case]
    got = await _plan(rows, settings_overrides, manager_overrides)
    text = json.dumps(got, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path = GOLDEN / f"{case}.json"
    if UPDATE:
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.is_file(), f"no golden file for {case}; run with SLOTSERVE_UPDATE_GOLDEN=1"
    assert json.loads(text) == json.loads(path.read_text(encoding="utf-8")), f"container spec changed: {case}"


@pytest.mark.parametrize("case", sorted(cases()))
async def test_every_case_is_hardened(case, monkeypatch):
    """The security fields are in every golden body, and nothing harden() refuses is."""
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n=None: TOKEN)
    config = (await _plan(*cases()[case]))["config"]
    host = config["HostConfig"]
    assert check(config) == []
    assert host["PidsLimit"] > 0 and host["Memory"] > 0 and host["LogConfig"]["Type"] == "json-file"
    assert all(c in host["CapDrop"] for c in ("NET_RAW", "MKNOD", "SYS_CHROOT", "AUDIT_WRITE", "SETFCAP"))
    assert not host.get("Privileged") and host["NetworkMode"] != "host"
    assert all(m.get("Source") != "/" for m in host.get("Mounts", []))
    assert all(r.get("Count") != -1 and r.get("DeviceIDs") for r in host.get("DeviceRequests", []))


async def test_a_lane_without_gpus_cannot_start_a_gpu_environment(monkeypatch):
    """No more "all GPUs" (Count -1): the person gets an error an admin can fix (give the lane its GPUs)."""
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n=None: TOKEN)
    with pytest.raises(DevEnvError) as caught:
        await _plan([builtin("pytorch", id=1), *booking(lane_id=3)], {}, {})
    assert caught.value.code == "devenv_lane_no_gpus" and caught.value.params == {"lane": "any GPU"}


def test_every_golden_file_has_a_case():
    assert sorted(p.stem for p in GOLDEN.glob("*.json")) == sorted(cases())

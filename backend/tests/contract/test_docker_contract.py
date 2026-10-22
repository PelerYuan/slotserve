"""Docker contract (A10): what SlotServe's Docker client gets from the fake equals what it gets from real Docker.

Each case runs twice (``backend`` = fake / real, see conftest.py). Add a case here whenever the fake learns
something new, and a row to ``services/docker_endpoints.py`` whenever SlotServe makes a new Docker call.
"""

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from slotserve.services import docker_endpoints
from slotserve.services.devenv import SshBundle, booking_ssh_files
from slotserve.services.devenv.ssh import generate_host_key, host_public_key
from slotserve.services.docker_api import DockerError, tar_files
from slotserve.services.shacrypt import sha512_crypt

from .docker_backends import GPU, free_port

GOLDEN = Path(__file__).resolve().parents[1] / "unit" / "containers" / "golden"
MiB = 1024**2

endpoints = pytest.mark.docker_endpoints


@endpoints("system.version", "system.info")
async def test_version_and_info(backend):
    version = await backend.docker.version()
    assert version["ApiVersion"] and version["Version"]
    info = await backend.docker.info()
    assert info["NCPU"] >= 1 and info["MemTotal"] > 0


@endpoints("images.pull", "images.inspect")
async def test_pull_and_inspect_image(backend):
    seen: list[str] = []

    async def progress(pct, status):
        seen.append(status)

    await backend.docker.pull(backend.image, progress)
    assert seen  # some progress was reported
    assert await backend.docker.image_exists(backend.image)
    assert not await backend.docker.image_exists("slotserve-contract-absent:none")


@endpoints("images.pull")
async def test_pull_of_a_missing_image_fails(backend):
    with pytest.raises(DockerError):
        await backend.docker.pull(f"slotserve-contract-missing-{backend.run}:missing")


@endpoints("containers.create")
async def test_create_needs_the_image(backend):
    with pytest.raises(DockerError) as err:
        await backend.docker.create(backend.name("x"), {**backend.container(), "Image": "slotserve-contract-absent:no"})
    assert err.value.status == 404


@endpoints("containers.create", "containers.remove")
async def test_container_names_are_unique(backend):
    name = backend.name("dup")
    await backend.docker.create(name, backend.container())
    with pytest.raises(DockerError) as err:
        await backend.docker.create(name, backend.container())
    assert err.value.status == 409
    await backend.docker.remove(name)  # by name
    await backend.docker.create(name, backend.container())


@endpoints(
    "containers.create",
    "containers.start",
    "containers.inspect",
    "containers.list",
    "containers.stop",
    "containers.remove",
)
async def test_container_lifecycle(backend):
    name = backend.name("life")
    cid = await backend.docker.create(name, backend.container(Labels=backend.labels(**{"slotserve.kind": "x"})))
    state = await backend.docker.inspect(cid)
    assert state["Name"] == "/" + name and state["State"]["Running"] is False
    await backend.docker.start(cid)
    state = await backend.docker.inspect(name)
    assert state["Id"].startswith(cid) and state["State"]["Running"] is True
    listed = await backend.docker.list(f"slotserve.contract={backend.run}")
    assert [(c["Id"], c["State"], c["Labels"]["slotserve.kind"]) for c in listed] == [(state["Id"], "running", "x")]
    await backend.docker.stop(cid, timeout=2)
    assert (await backend.docker.inspect(cid))["State"]["Running"] is False
    await backend.docker.remove(cid)
    assert await backend.docker.inspect(cid) is None
    await backend.docker.remove(cid)  # already gone: no error
    await backend.docker.stop(cid)  # likewise


@endpoints("containers.create", "containers.start", "containers.archive_put", "containers.exec", "exec.start")
@endpoints("exec.inspect")
async def test_copied_files_and_exec(backend):
    cid = await backend.docker.create(backend.name("files"), backend.container())
    await backend.docker.start(cid)
    files = {"tmp/contract/hello.txt": (b"hello from slotserve", 0o640)}
    await backend.docker.put_archive(cid, "/", tar_files(files))
    backend.exec_returns(0, "hello from slotserve")
    assert await backend.docker.exec(cid, ["cat", "/tmp/contract/hello.txt"]) == (0, "hello from slotserve")
    backend.exec_returns(0, "640\n")
    code, out = await backend.docker.exec(cid, ["stat", "-c", "%a", "/tmp/contract/hello.txt"])
    assert (code, out.strip()) == (0, "640")
    backend.exec_returns(3, "")
    assert (await backend.docker.exec(cid, ["sh", "-c", "exit 3"]))[0] == 3


@endpoints("containers.create", "containers.exec")
async def test_exec_needs_a_running_container(backend):
    cid = await backend.docker.create(backend.name("stopped"), backend.container())
    with pytest.raises(DockerError) as err:
        await backend.docker.exec(cid, ["true"])
    assert err.value.status == 409


@endpoints("containers.create", "containers.start", "containers.exec", "exec.start")
async def test_exec_in_the_background(backend):
    cid = await backend.docker.create(backend.name("bg"), backend.container())
    await backend.docker.start(cid)
    assert await backend.docker.exec_detached(cid, ["sleep", "1"])


@endpoints("containers.create", "containers.start", "containers.wait", "containers.logs")
async def test_helper_exit_code_and_logs(backend):
    """How storage and devenv helpers run: a script, wait for its exit code, read what it printed."""
    script = "echo contract-hello; echo to-stderr >&2; exit 3"
    backend.helper_prints("echo contract-hello", "contract-hello\nto-stderr\n", 3)
    cid = await backend.docker.create(backend.name("helper"), backend.container(entrypoint=["/bin/sh", "-c", script]))
    await backend.docker.start(cid)
    assert await backend.docker.wait(cid, timeout=60) == 3
    logs = await backend.docker.logs(cid)
    assert "contract-hello" in logs and "to-stderr" in logs


@endpoints("containers.create", "containers.start", "containers.stats", "containers.update", "containers.remove")
async def test_memory_limit_stats_and_update(backend):
    host = {"Memory": 64 * MiB, "MemorySwap": 128 * MiB, "CpusetCpus": "0"}
    cid = await backend.docker.create(backend.name("limits"), backend.container(host=host))
    await backend.docker.start(cid)
    stats = await backend.docker.stats(cid)
    assert isinstance(stats["cpu_stats"]["cpu_usage"]["total_usage"], int)
    assert stats["memory_stats"]["usage"] > 0 and stats["memory_stats"]["limit"] == 64 * MiB
    await backend.docker.update(
        cid, {"CpusetCpus": "0", "NanoCpus": 10**9, "Memory": 96 * MiB, "MemorySwap": 192 * MiB}
    )
    assert (await backend.docker.stats(cid))["memory_stats"]["limit"] == 96 * MiB
    await backend.docker.remove(cid)
    assert await backend.docker.stats(cid) is None


@endpoints("containers.create", "containers.start", "containers.exec", "exec.start", "exec.inspect")
async def test_cpuset_limits_what_the_container_sees(backend):
    host = {"CpusetCpus": "0", "NanoCpus": 10**9, "Memory": 128 * MiB}
    cid = await backend.docker.create(backend.name("cpuset"), backend.container(host=host))
    await backend.docker.start(cid)
    backend.exec_returns(0, "1\n")
    code, out = await backend.docker.exec(cid, ["nproc"])
    assert (code, out.strip()) == (0, "1")


@endpoints("containers.create", "containers.start", "containers.inspect")
async def test_docker_picks_a_free_host_port(backend):
    host = {"PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}}
    cid = await backend.docker.create(backend.name("web"), backend.container(ExposedPorts={"8080/tcp": {}}, host=host))
    await backend.docker.start(cid)
    binding = (await backend.docker.inspect(cid))["NetworkSettings"]["Ports"]["8080/tcp"]
    assert any(b["HostIp"] == "127.0.0.1" and b["HostPort"].isdigit() for b in binding)


@endpoints("containers.create", "containers.start")
async def test_a_taken_host_port_fails_the_start(backend):
    port = free_port()
    await backend.occupy_port(port)
    host = {"PortBindings": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]}}
    cid = await backend.docker.create(backend.name("ssh"), backend.container(ExposedPorts={"22/tcp": {}}, host=host))
    with pytest.raises(DockerError) as err:
        await backend.docker.start(cid)
    assert "port is already allocated" in err.value.message


@endpoints("containers.create", "containers.start", "containers.remove")
async def test_create_on_a_missing_network(backend):
    # Real Docker (28.x) accepts the create and only fails at start; either way the container never runs.
    body = backend.container(host={"NetworkMode": backend.name("no-such-network")})
    name = backend.name("lost")
    with pytest.raises(DockerError) as err:
        await backend.docker.create(name, body)
        try:
            await backend.docker.start(name)
        finally:
            await backend.docker.remove(name)
    assert err.value.status == 404


@endpoints(
    "networks.create",
    "networks.list",
    "networks.connect",
    "networks.disconnect",
    "networks.remove",
    "containers.create",
    "containers.start",
    "containers.inspect",
    "containers.remove",
)
async def test_network_lifecycle(backend):
    first, second = backend.name("net1"), backend.name("net2")
    await backend.docker.network_create(first, backend.labels(**{"slotserve.session": "1"}))
    await backend.docker.network_create(second, backend.labels())
    with pytest.raises(DockerError) as err:
        await backend.docker.network_create(first, backend.labels())
    assert err.value.status == 409
    listed = await backend.docker.networks("slotserve.session=1")
    assert first in [n["Name"] for n in listed] and second not in [n["Name"] for n in listed]

    cid = await backend.docker.create(backend.name("member"), backend.container(host={"NetworkMode": first}))
    await backend.docker.start(cid)
    await backend.docker.network_connect(second, cid, aliases=["contract-alias"])
    await backend.docker.network_connect(second, cid)  # already connected: no error
    networks = (await backend.docker.inspect(cid))["NetworkSettings"]["Networks"]
    assert {first, second} <= set(networks)
    await backend.docker.network_disconnect(second, cid)
    assert second not in (await backend.docker.inspect(cid))["NetworkSettings"]["Networks"]
    with pytest.raises(DockerError):
        await backend.docker.network_remove(first)  # still has a member
    await backend.docker.remove(cid)
    await backend.docker.network_remove(first)
    await backend.docker.network_remove(first)  # gone: no error
    await backend.docker.network_remove(second)


@endpoints("volumes.create", "volumes.inspect", "volumes.list", "volumes.remove")
async def test_volume_lifecycle(backend):
    name = backend.name("vol")
    created = await backend.docker.volume_create(name, labels=backend.labels(**{"slotserve.storage": "7"}))
    assert created["Name"] == name and created["Driver"] == "local"
    seen = await backend.docker.volume_inspect(name)
    assert seen["Labels"]["slotserve.storage"] == "7"
    assert name in [v["Name"] for v in await backend.docker.volumes("slotserve.storage=7")]
    assert name in [v["Name"] for v in await backend.docker.volumes("slotserve.storage")]  # key only
    await backend.docker.remove_volume(name)
    assert await backend.docker.volume_inspect(name) is None
    await backend.docker.remove_volume(name)  # gone: no error


@endpoints(
    "containers.create",
    "containers.start",
    "containers.remove",
    "volumes.inspect",
    "volumes.remove",
    "containers.exec",
    "exec.start",
    "exec.inspect",
)
async def test_a_mounted_volume_is_kept_while_in_use(backend):
    vol = backend.name("ws")
    host = {"Mounts": [{"Type": "volume", "Source": vol, "Target": "/workspace"}]}
    cid = await backend.docker.create(backend.name("user"), backend.container(host=host, WorkingDir="/workspace"))
    await backend.docker.start(cid)
    assert await backend.docker.volume_inspect(vol) is not None  # created by Docker for the mount
    backend.exec_returns(0, "ok\n")
    assert (await backend.docker.exec(cid, ["sh", "-c", "touch /workspace/f && echo ok"]))[1].strip() == "ok"
    with pytest.raises(DockerError) as err:
        await backend.docker.remove_volume(vol)
    assert err.value.status == 409
    await backend.docker.remove(cid)
    await backend.docker.remove_volume(vol)


@endpoints("containers.create", "containers.start", "images.commit", "images.inspect", "images.remove")
async def test_save_as_image(backend):
    cid = await backend.docker.create(backend.name("save"), backend.container())
    await backend.docker.start(cid)
    ref = f"slotserve-contract/saved-{backend.run}:1"
    backend.images.append(ref)
    assert await backend.docker.commit(cid, ref, comment="contract")
    assert await backend.docker.image_exists(ref)
    await backend.docker.remove_image(ref)
    assert not await backend.docker.image_exists(ref)
    await backend.docker.remove_image(ref)  # gone: no error


@endpoints("containers.create", "containers.start", "containers.exec", "exec.start", "exec.inspect")
async def test_gpu_device_request(backend):
    if backend.real and not GPU:
        pytest.skip("no GPU here (set SLOTSERVE_TEST_GPU=1 on a machine with the NVIDIA container toolkit)")
    host = {"DeviceRequests": [{"Driver": "nvidia", "DeviceIDs": ["0"], "Capabilities": [["gpu"]]}]}
    body = backend.container(host=host, Image="ubuntu:24.04") if backend.real else backend.container(host=host)
    if backend.real and not await backend.docker.image_exists("ubuntu:24.04"):
        await backend.docker.pull("ubuntu:24.04")
    cid = await backend.docker.create(backend.name("gpu"), body)
    await backend.docker.start(cid)
    backend.exec_returns(0, "GPU 0: Fake GPU (UUID: GPU-0)\n")
    code, out = await backend.docker.exec(cid, ["nvidia-smi", "-L"])
    assert code == 0 and len([line for line in out.splitlines() if line.startswith("GPU ")]) == 1


# ---- seeding a new folder from an image without running it (containers/helpers.py, R1-06) -----------------------


@endpoints("containers.create", "containers.archive_get", "containers.archive_put", "containers.start")
@endpoints("containers.wait", "containers.logs", "containers.inspect")
async def test_copy_a_directory_out_of_an_image_that_never_runs(backend):
    """A container of the source image is only created; Docker's archive API reads a directory from it into a
    helper, keeping ownership (copyUIDGID). A path the image does not have is a 404 (no seeding)."""
    if backend.fake is not None:
        backend.fake.image_files[backend.image] = {"/etc/contract/seed.txt": b"seeded"}
    source = await backend.docker.create(backend.name("src"), backend.container(entrypoint=["/bin/true"]))
    script = "cat /tmp/seed/contract/seed.txt" if backend.fake is not None else "ls /tmp/seed/etc/passwd"
    backend.helper_prints(script, "seeded", 0)
    helper = await backend.docker.create(backend.name("dst"), backend.container(entrypoint=["/bin/sh", "-c", script]))
    await backend.docker.put_archive(helper, "/", tar_files({"tmp/seed/.keep": (b"", 0o600)}))
    path = "/etc/contract" if backend.fake is not None else "/etc"
    assert await backend.docker.copy_path(source, path, helper, "/tmp/seed")
    assert not await backend.docker.copy_path(source, "/slotserve-contract-absent", helper, "/tmp/seed")
    await backend.docker.start(helper)
    assert await backend.docker.wait(helper, timeout=60) == 0
    assert (await backend.docker.inspect(source))["State"]["Running"] is False  # the image never ran


# ---- SSH into a dev environment built from the golden container spec (replaces CI's inline script) ----------------

SSH_IMAGE = "ubuntu:24.04"
PASSWORD = "ci-secret-pw"


def _ssh_opts(known_hosts: Path) -> list[str]:
    """The host key is checked: it is the environment's own (R1-07)."""
    return ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=5"]


def _ssh_spec(backend, golden: dict, port: int, network: str) -> dict:
    """The golden booking container, made runnable on a GPU-less CI runner: same entrypoint, user, workdir, init,
    restart policy and labels; the image, GPU, models mount, CPU set and network adapted to this host."""
    config = json.loads(json.dumps(golden["config"]))
    host = config["HostConfig"]
    if not GPU:
        host.pop("DeviceRequests", None)
    host["CpusetCpus"] = "0"
    host["Mounts"] = [{"Type": "volume", "Source": backend.name("ws"), "Target": "/workspace"}]
    host["NetworkMode"] = network
    host["PortBindings"] = {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
    config["Image"] = SSH_IMAGE if backend.real else backend.image
    config["Env"] = [*config["Env"], "FROM_IMAGE=yes"]
    config["Labels"] = {**config["Labels"], **backend.labels()}
    return config


def _ssh(known: Path, *args: str, password: str | None = None, key: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["ssh", *_ssh_opts(known)]
    if key is not None:
        cmd += ["-o", "BatchMode=yes", "-i", str(key)]
    else:
        cmd = ["sshpass", "-p", password or "", *cmd, "-o", "PubkeyAuthentication=no"]
    return subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=30)


@endpoints(
    "networks.create",
    "containers.create",
    "containers.archive_put",
    "containers.start",
    "containers.inspect",
    "containers.logs",
)
async def test_ssh_login_into_a_dev_environment(backend, tmp_path):
    """Key login, environment variables reach the session, password login, a wrong password is refused."""
    golden = json.loads((GOLDEN / "booking_gpu_keys_and_password.json").read_text(encoding="utf-8"))
    dropbear = os.environ.get("SLOTSERVE_TEST_DROPBEAR", "")
    port = free_port()
    if backend.real:
        if sys.platform == "win32" or not Path(dropbear).is_file():
            pytest.skip("needs the Dropbear binary of the image (SLOTSERVE_TEST_DROPBEAR) on Linux")
        if not (shutil.which("ssh") and shutil.which("ssh-keygen") and shutil.which("sshpass")):
            pytest.skip("needs ssh, ssh-keygen and sshpass")
        if not await backend.docker.image_exists(SSH_IMAGE):
            await backend.docker.pull(SSH_IMAGE)
        keygen = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "client")]
        await asyncio.to_thread(subprocess.run, keygen, check=True)
        # the environment's own host key, made by SlotServe's generator (Dropbear's file format), checked strictly
        host_key = generate_host_key()
        known = tmp_path / "known_hosts"
        known.write_text(f"[127.0.0.1]:{port} {host_public_key(host_key)}" + chr(10))
        bundle = SshBundle(Path(dropbear).read_bytes(), host_key)
        client_key = (tmp_path / "client.pub").read_text().strip()
    else:
        bundle = SshBundle(b"\x7fELF fake dropbear", b"fake host key", "")
        client_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIContractContractContractContractContrac contract"

    network = backend.name("devenv")
    await backend.docker.network_create(network, backend.labels())
    cid = await backend.docker.create(backend.name("devenv"), _ssh_spec(backend, golden, port, network))
    # what SlotServe copies into a booking container (the same files and tar as services/devenv)
    await backend.docker.put_archive(
        cid, "/", tar_files(booking_ssh_files(bundle, [client_key], sha512_crypt(PASSWORD)))
    )
    await backend.docker.start(cid)
    assert (await backend.docker.inspect(cid))["State"]["Running"]
    if not backend.real:
        assert "opt/slotserve/ssh/authorized_keys" in backend.fake.archives[cid]
        return

    host = ["-p", str(port), "root@127.0.0.1"]
    for _ in range(40):
        if _ssh(known, *host, "true", key=tmp_path / "client").returncode == 0:
            break
        await asyncio.sleep(0.5)
    out = _ssh(known, *host, 'echo LOGIN_OK; echo "env=$FROM_IMAGE"; id -u', key=tmp_path / "client")
    logs = await backend.docker.logs(cid)
    assert out.returncode == 0, out.stderr + logs
    assert out.stdout.split() == ["LOGIN_OK", "env=yes", "0"], out.stdout
    assert "PASSWORD_OK" in _ssh(known, *host, "echo PASSWORD_OK", password=PASSWORD).stdout
    assert _ssh(known, *host, "true", password="wrong-password").returncode != 0, "a wrong password must be refused"


# ---- coverage of the endpoint table -----------------------------------------------------------------------------


def test_every_endpoint_has_a_contract_case():
    covered: set[str] = set()
    cases = 0
    for name, func in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
        marks = [m for m in getattr(func, "pytestmark", []) if m.name == "docker_endpoints"]
        if name.startswith("test_") and marks:
            cases += 1
            covered.update(n for m in marks for n in m.args)
    assert covered <= set(docker_endpoints.BY_NAME), sorted(covered - set(docker_endpoints.BY_NAME))
    assert set(docker_endpoints.BY_NAME) <= covered, (
        f"no contract case: {sorted(set(docker_endpoints.BY_NAME) - covered)}"
    )
    assert cases >= 15

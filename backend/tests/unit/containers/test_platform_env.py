"""F11 / A13 / R1-11: the platform contributor, the image cleaning before a commit and where the container-side gateway
listens (pure parts; tests/devenv/test_platform_context.py runs them against the fake Docker)."""

import io
import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta

import pytest

from slotserve.containers.context import LaneInfo, PlatformInfo, SessionContext, TemplateInfo, UserInfo
from slotserve.containers.contributors.platform_env import (
    ENV_FILE,
    KEY_FILE,
    MOTD_FILE,
    PROFILE_FILE,
    PlatformEnvContributor,
    end_epoch,
    env_file,
    motd_script,
)
from slotserve.containers.image_clean import committed_env, lock_root, original_shadow
from slotserve.containers.model import ContainerSpec
from slotserve.gateway.container_gw import Placement, choose

URL = "http://slotserve-gw:8002/v1"
KEY = "sk-ss-env-unitkeyunitkeyunitkey"


def ctx(kind: str = "booking", platform: PlatformInfo | None = None, end_at: str = "", **extra) -> SessionContext:
    return SessionContext(
        session_id=7,
        kind=kind,
        image="img",
        ssh=None,
        resources=None,
        node_policy=None,
        web={"ports": [], "token": "t"},
        user=None if kind == "shared" else UserInfo(1, "alice"),
        lane=LaneInfo(1, "Lane A", [0, 1]),
        end_at=end_at,
        platform=platform,
        **extra,
    )


def spec_for(c: SessionContext, env: dict | None = None) -> ContainerSpec:
    spec = ContainerSpec(name="x", image="img", env=dict(env or {}))
    PlatformEnvContributor().contribute(c, spec)
    return spec


def test_off_without_platform():
    spec = spec_for(ctx())
    assert spec.env == {} and spec.files == {} and spec.host_extra == {}


def test_key_is_a_0600_file_never_env():
    p = PlatformInfo(llm_url=URL, extra_host="slotserve-gw:172.17.0.1", llm_key=KEY, env_name="thesis", machine="box")
    spec = spec_for(ctx(platform=p, end_at="2026-01-05T17:30:00+00:00"))
    assert spec.files[KEY_FILE] == (KEY.encode(), 0o600)
    assert not any(KEY in v for v in spec.env.values()) and "OPENAI_API_KEY" not in spec.env
    assert spec.env["OPENAI_BASE_URL"] == URL and spec.env["SLOTSERVE_LLM_URL"] == URL
    assert spec.env["SLOTSERVE_ENV_ID"] == "7" and spec.env["SLOTSERVE_GPUS"] == "0,1"
    assert spec.env["SLOTSERVE_LANE"] == "Lane A" and spec.env["SLOTSERVE_MACHINE"] == "box"
    assert spec.host_extra["ExtraHosts"] == ["slotserve-gw:172.17.0.1"]
    assert spec.files[ENV_FILE][1] == spec.files[MOTD_FILE][1] == spec.files[PROFILE_FILE][1] == 0o644
    profile = spec.files[PROFILE_FILE][0].decode()
    assert 'OPENAI_API_KEY="$(cat /run/slotserve/llm-key)"' in profile and KEY not in profile
    assert "try: python3 -c" in spec.files[MOTD_FILE][0].decode()


def test_shared_environment_gets_no_key():
    p = PlatformInfo(llm_url=URL, llm_key=KEY, env_name="idle")
    spec = spec_for(ctx("shared", platform=p))
    assert KEY_FILE not in spec.files and spec.env["OPENAI_BASE_URL"] == URL
    assert "own API key" in spec.files[MOTD_FILE][0].decode()
    assert "SLOTSERVE_CHECKPOINT_DIR" not in spec.env  # no /workspace there
    assert b"SLOTSERVE_END" not in spec.files[ENV_FILE][0]


def test_no_endpoint_no_key():
    spec = spec_for(ctx(platform=PlatformInfo(llm_url="", llm_key=KEY, env_name="x")))
    assert KEY_FILE not in spec.files and "OPENAI_BASE_URL" not in spec.env and "ExtraHosts" not in spec.host_extra
    assert b"LLM" not in spec.files[MOTD_FILE][0]


def test_template_endpoint_wins():
    p = PlatformInfo(llm_url=URL, llm_key=KEY)
    spec = spec_for(ctx(platform=p, template=TemplateInfo(id=1)), env={"OPENAI_BASE_URL": "https://api.example/v1"})
    assert spec.env["OPENAI_BASE_URL"] == "https://api.example/v1" and spec.env["SLOTSERVE_LLM_URL"] == URL


def test_cpu_environment_has_no_gpus():
    spec = spec_for(ctx("cpu", platform=PlatformInfo(llm_url=URL, language="zh-CN")))
    assert spec.env["SLOTSERVE_GPUS"] == "" and spec.env["SLOTSERVE_LANE"] == ""
    assert "仅 CPU" in spec.files[MOTD_FILE][0].decode()


def test_end_and_env_file():
    assert end_epoch("2026-01-05T17:30:00+00:00") == end_epoch("2026-01-05T17:30:00") == 1767634200
    assert end_epoch("") is None and end_epoch("garbage") is None
    text = env_file("2026-01-05T17:30:00+00:00", "2026-01-05 18:30 Europe/Berlin").decode()
    assert "export SLOTSERVE_END_EPOCH=1767634200" in text
    assert "export SLOTSERVE_END_LABEL='2026-01-05 18:30 Europe/Berlin'" in text


@pytest.mark.skipif(shutil.which("sh") is None or os.name == "nt", reason="needs a POSIX shell")
@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_banner_runs_and_quotes_names(tmp_path, language):
    """At most three lines; names from people never run as code."""
    end = (datetime.now(UTC) + timedelta(hours=1, minutes=12, seconds=30)).isoformat()
    marker = tmp_path / "pwned"
    p = PlatformInfo(llm_url=URL, env_name=f"x'$(touch {marker})\"`touch {marker}`", machine="box", language=language)
    c = ctx(platform=p, end_at=end)
    script = tmp_path / "banner.sh"
    script.write_bytes(env_file(end, "18:00 UTC") + motd_script(c, p, with_key=True))
    out = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=10, check=True).stdout
    lines = out.splitlines()
    assert 1 <= len(lines) <= 3 and not marker.exists()
    assert "1h12m" in lines[0] and "18:00 UTC" in lines[0] and "GPU 0,1" in lines[0]
    assert lines[-1].startswith("LLM") and URL in lines[-1]


# ---- image cleaning (R1-11) ------------------------------------------------------------------------------------


def test_committed_env_keeps_the_image_and_empties_the_rest():
    image = ["PATH=/usr/bin", "LANG=C.UTF-8"]
    container = [*image, "SLOTSERVE_END=x", "OPENAI_BASE_URL=http://slotserve-gw:8002/v1", "PLAIN=template"]
    assert committed_env(container, image) == [*image, "SLOTSERVE_END=", "OPENAI_BASE_URL=", "PLAIN="]
    # the image overrode a variable at create: the image's value comes back
    assert committed_env(["PATH=/opt/bin", "LANG=C"], ["PATH=/usr/bin"]) == ["PATH=/usr/bin", "LANG="]
    # the image is gone: only SlotServe's own variables are emptied
    got = committed_env(["PATH=/usr/bin", "SLOTSERVE_USER=alice", "JUPYTER_TOKEN=t", "PLAIN=x"], None)
    assert got == ["PATH=/usr/bin", "SLOTSERVE_USER=", "JUPYTER_TOKEN=", "PLAIN=x"]


def _shadow(text: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("shadow")
        info.size, info.mode, info.uid, info.gid = len(text), 0o640, 0, 42
        tar.addfile(info, io.BytesIO(text))
    return buf.getvalue()


def _read(archive: bytes) -> tuple[tarfile.TarInfo, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.getmembers()[0]
        return member, tar.extractfile(member).read()


def test_lock_root_keeps_owner_and_other_accounts():
    text = b"root:$6$salt$hash:19000:0:99999:7:::\ndaemon:*:19000:0:99999:7:::\n"
    locked, changed = lock_root(_shadow(text))
    member, data = _read(locked)
    assert changed and member.name == "etc/shadow" and member.mode == 0o640 and member.gid == 42
    assert data == b"root:*:19000:0:99999:7:::\ndaemon:*:19000:0:99999:7:::\n"
    assert lock_root(_shadow(b"root:*:1::::::\n"))[1] is False
    assert _read(original_shadow(_shadow(text)))[1] == text


# ---- where the container-side gateway listens ------------------------------------------------------------------


def test_gateway_placement():
    # on the host: the bridge address, in /etc/hosts of the environments
    assert choose("", "0.0.0.0", False, True, "172.17.0.1") == Placement("172.17.0.1", "slotserve-gw:172.17.0.1")
    # in a container: all its addresses, found through the network alias
    assert choose("", "0.0.0.0", True, True, None) == Placement("0.0.0.0", None)
    # set by the admin
    assert choose("10.0.0.5", "0.0.0.0", False, True, "172.17.0.1") == Placement("10.0.0.5", "slotserve-gw:10.0.0.5")
    assert choose("0.0.0.0", "127.0.0.1", False, True, None) == Placement("0.0.0.0", "slotserve-gw:host-gateway")
    # no bridge: SlotServe's own address, unless that is loopback
    assert choose("", "0.0.0.0", False, True, None) == Placement("0.0.0.0", "slotserve-gw:host-gateway")
    assert choose("", "127.0.0.1", False, True, None) is None
    # Docker on another machine: its containers cannot reach this one's bridge
    assert choose("", "0.0.0.0", False, False, "172.17.0.1") is None

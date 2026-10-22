"""harden() is the last step before Docker and cannot be skipped (A01 acceptance 1, R1-05, D14)."""

import copy

import httpx
import pytest

from slotserve.containers import build
from slotserve.containers.harden import CAP_DROP, HardenError, check, harden
from slotserve.containers.helpers import helper_body, helper_spec
from slotserve.containers.model import ContainerSpec, GiB, NodePolicy
from slotserve.containers.render import render
from slotserve.services.docker_api import DockerClient, UnhardenedSpec

POLICY = NodePolicy(host_memory=64 * GiB)


def plain() -> dict:
    return render(
        ContainerSpec(name="x", image="example/img:1", mounts=[{"Type": "volume", "Source": "v", "Target": "/w"}])
    )


def test_defaults():
    body = harden(plain(), kind="booking", node_policy=POLICY)
    host = body["HostConfig"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["CapDrop"] == list(CAP_DROP)
    assert host["PidsLimit"] == 4096
    # host memory less max(4 GB, 10 %)
    assert host["Memory"] == 64 * GiB - int(64 * GiB * 0.10)
    assert NodePolicy(host_memory=16 * GiB).default_memory() == 12 * GiB
    assert host["Ulimits"] == [{"Name": "nofile", "Soft": 1048576, "Hard": 1048576}]
    assert host["LogConfig"] == {"Type": "json-file", "Config": {"max-size": "50m", "max-file": "5"}}
    assert body["Labels"]["slotserve.hardened"] == "1" and check(body) == []


def test_idempotent_and_pure():
    raw = plain()
    before = copy.deepcopy(raw)
    once = harden(raw, kind="booking", node_policy=POLICY)
    assert raw == before  # the input is not changed
    assert harden(once, kind="booking", node_policy=POLICY) == once


def test_keeps_stricter_values_and_caps_looser_ones():
    raw = plain()
    raw["HostConfig"].update(Memory=2 * GiB, PidsLimit=100, CapDrop=["ALL"])
    host = harden(raw, kind="forwarder", node_policy=POLICY)["HostConfig"]
    assert host["Memory"] == 2 * GiB and host["PidsLimit"] == 100 and host["CapDrop"] == ["ALL"]
    raw["HostConfig"]["PidsLimit"] = 10**6
    assert harden(raw, kind="booking", node_policy=POLICY)["HostConfig"]["PidsLimit"] == 4096


@pytest.mark.parametrize(
    "change",
    [
        {"Privileged": True},
        {"CapAdd": ["SYS_ADMIN"]},
        {"NetworkMode": "host"},
        {"PidMode": "host"},
        {"IpcMode": "host"},
        {"UsernsMode": "host"},
        {"Binds": ["/:/host"]},
        {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
        {"Mounts": [{"Type": "bind", "Source": "/", "Target": "/host"}]},
        {"Mounts": [{"Type": "bind", "Source": "/run/docker.sock", "Target": "/s"}]},
        {"Devices": [{"PathOnHost": "/dev/sda"}]},
    ],
)
def test_refuses_what_no_container_may_have(change):
    raw = plain()
    raw["HostConfig"].update(change)
    with pytest.raises(HardenError) as caught:
        harden(raw, kind="booking", node_policy=POLICY)
    assert caught.value.code == "container_forbidden"
    assert check(raw)  # and DockerClient.create would refuse it anyway


def test_all_gpus_is_refused():
    raw = plain()
    raw["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]
    with pytest.raises(HardenError) as caught:
        harden(raw, kind="booking", node_policy=POLICY)
    assert caught.value.code == "devenv_lane_no_gpus"


def test_memory_must_be_known():
    with pytest.raises(HardenError) as caught:
        harden(plain(), kind="booking", node_policy=NodePolicy())
    assert caught.value.code == "devenv_memory_unknown"


def test_compat_mode_only_drops_no_new_privileges():
    body = harden(plain(), kind="booking", node_policy=POLICY, compat=True)
    host = body["HostConfig"]
    assert "SecurityOpt" not in host and body["Labels"]["slotserve.compat"] == "1"
    assert host["CapDrop"] == list(CAP_DROP) and host["PidsLimit"] and host["Memory"] and check(body) == []
    # helpers and forwarders never run in compatibility mode
    helper = harden(plain(), kind="helper", node_policy=POLICY, compat=True)
    assert (
        helper["HostConfig"]["SecurityOpt"] == ["no-new-privileges:true"] and "slotserve.compat" not in helper["Labels"]
    )


def test_harden_runs_after_every_contributor(monkeypatch):
    """A contributor (even one added at the end of the list) cannot loosen the result: harden() comes after it."""

    class Loosen:
        name = "loosen"

        def contribute(self, ctx, spec):
            spec.host_extra.update(SecurityOpt=[], CapDrop=[], PidsLimit=0, LogConfig={"Type": "none"})
            spec.memory = None

    class Escalate:
        name = "escalate"

        def contribute(self, ctx, spec):
            spec.host_extra["Privileged"] = True

    spec = ContainerSpec(name="x", image="img")
    monkeypatch.setattr(build, "CONTRIBUTORS", [Loosen()])
    for c in build.CONTRIBUTORS:
        c.contribute(None, spec)
    host = build.render_for_docker(spec, POLICY)["HostConfig"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"] and host["CapDrop"] == list(CAP_DROP)
    assert host["PidsLimit"] == 4096 and host["Memory"] > 0 and host["LogConfig"]["Type"] == "json-file"
    Escalate().contribute(None, spec)
    with pytest.raises(HardenError):
        build.render_for_docker(spec, POLICY)


def test_helpers_are_hardened():
    body = helper_body(helper_spec("busybox:1.37", "true", label_key="slotserve.storage"))
    assert check(body) == [] and body["HostConfig"]["Memory"] == GiB and body["HostConfig"]["PidsLimit"] == 512


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(201, json={"Id": "abc"})


async def test_docker_client_refuses_what_did_not_go_through_harden():
    """The guard sits in DockerClient.create itself: no code path can skip harden()."""
    transport = Recorder()
    docker = DockerClient("unix:///fake.sock", transport=transport)
    try:
        with pytest.raises(UnhardenedSpec) as caught:
            await docker.create("raw", plain())
        assert "not hardened" in caught.value.found
        # hardened, then loosened afterwards: refused too
        loosened = harden(plain(), kind="booking", node_policy=POLICY)
        loosened["HostConfig"]["Privileged"] = True
        del loosened["HostConfig"]["PidsLimit"]
        with pytest.raises(UnhardenedSpec) as caught:
            await docker.create("loosened", loosened)
        assert "Privileged" in caught.value.found and "PidsLimit missing" in caught.value.found
        assert transport.requests == []  # nothing reached Docker
        assert await docker.create("ok", harden(plain(), kind="booking", node_policy=POLICY)) == "abc"
        assert len(transport.requests) == 1
    finally:
        await docker.close()

"""The endpoint table (services/docker_endpoints.py) and the fake Docker's refusal of anything undeclared."""

import httpx
import pytest
from docker_mock import FakeDocker

from slotserve.services import docker_endpoints as de


def test_paths_match_their_endpoints():
    assert de.find("GET", "/v1.41/images/registry.example:5000/team/img:1/json")[0].name == "images.inspect"
    assert de.find("DELETE", "/images/nvcr.io/nvidia/pytorch:25.01-py3")[0].name == "images.remove"
    assert de.find("GET", "/containers/json")[0].name == "containers.list"
    assert de.find("GET", "/containers/abc/json") == (de.BY_NAME["containers.inspect"], {"id": "abc"})
    assert de.find("POST", "/containers/abc/kill") is None
    assert de.find("GET", "/containers/create") is None
    assert len(de.BY_NAME) == len(de.ENDPOINTS)


def test_body_fields_are_declared():
    body = {
        "Image": "x",
        "Labels": {"slotserve.devenv": "1", "anything.goes": "here"},
        "HostConfig": {
            "Mounts": [{"Type": "bind", "Source": "/srv", "Target": "/x", "ReadOnly": True}],
            "PortBindings": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2200"}]},
            "DeviceRequests": [{"Driver": "nvidia", "DeviceIDs": ["0"], "Capabilities": [["gpu"]]}],
        },
    }
    assert de.check_request("POST", "/containers/create", ["name"], body) == (de.BY_NAME["containers.create"], [])
    body["HostConfig"]["Mounts"][0]["BindOptions"] = {"Propagation": "shared"}
    body["HostConfig"]["PortBindings"]["22/tcp"][0]["Extra"] = 1
    body["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    _, problems = de.check_request("POST", "/containers/create", ["name", "platform"], body)
    assert problems == [
        "query parameter 'platform' is not declared",
        "body field 'HostConfig.Mounts[].BindOptions' is not declared",
        "body field 'HostConfig.PortBindings.*[].Extra' is not declared",
        "body field 'HostConfig.CapAdd' is not declared",
    ]


@pytest.mark.parametrize(
    "host, problem",
    [
        ({"Privileged": True}, "HostConfig.Privileged"),
        ({"NetworkMode": "host"}, "HostConfig.NetworkMode"),
        ({"PidMode": "host"}, "HostConfig.PidMode"),
        ({"Binds": ["/:/host"]}, "HostConfig.Binds"),
        ({"Mounts": [{"Type": "bind", "Source": "/", "Target": "/host"}]}, "HostConfig.Mounts"),
    ],
)
def test_create_refuses_what_no_container_may_have(host, problem):
    problems = de.check_create({"Image": "x", "Labels": {"slotserve.devenv": "1"}, "HostConfig": host})
    assert problems and problems[0].startswith(problem)


def test_create_needs_a_slotserve_label():
    assert de.check_create({"Image": "x", "Labels": {"other": "1"}}) == ["Labels: a slotserve.* label is required"]
    assert de.check_create({"Image": "x", "Labels": {"slotserve.storage": "helper"}}) == []


async def test_the_fake_refuses_undeclared_requests():
    fake = FakeDocker()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://docker/v1.41") as c:
        assert (await c.get("/version")).status_code == 200
        r = await c.post("/containers/abc/kill")
        assert r.status_code == 400 and "not a declared Docker endpoint" in r.json()["message"]
        r = await c.post(
            "/containers/create",
            params={"name": "x"},
            json={"Image": "i", "Labels": {"slotserve.a": "1"}, "Volumes": {}},
        )
        assert r.status_code == 400 and "'Volumes'" in r.json()["message"]
        r = await c.get("/containers/json", params={"all": "true", "size": "1"})
        assert r.status_code == 400 and "'size'" in r.json()["message"]
        r = await c.post(
            "/containers/create",
            params={"name": "x"},
            json={"Image": "i", "Labels": {"slotserve.a": "1"}, "HostConfig": {"Privileged": True}},
        )
        assert r.status_code == 400
    assert fake.used == ["system.version"] and len(fake.rejected) == 4

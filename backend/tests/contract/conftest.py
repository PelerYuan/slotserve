"""L3 contract tests (A11): the same cases against the fake Docker and against a real Docker daemon.

``backend`` is parametrized ``fake`` / ``real``. ``real`` runs only with ``SLOTSERVE_TEST_DOCKER=1`` (CI job
``contract-docker``; ``DOCKER_HOST`` picks the daemon, default ``unix:///var/run/docker.sock``); otherwise it is
skipped. GPU cases also need ``SLOTSERVE_TEST_GPU=1`` (no GPU on GitHub's runners).

The fake does not run programs, so a case tells it what the real daemon would answer (``exec_returns``,
``helper_prints``, ...); for the real daemon these calls do nothing, and the real answer proves the script right.
Every case declares the Docker endpoints it uses (``@pytest.mark.docker_endpoints(...)``): in fake mode a request
to an undeclared one fails the case, and ``test_every_endpoint_has_a_contract_case`` checks that every row of
``services/docker_endpoints.py`` is covered.
"""

import os

import httpx
import pytest
from docker_backends import IMAGE, REAL, Backend
from docker_mock import FakeDocker

from slotserve.services.docker_api import DockerClient


def pytest_configure(config):
    config.addinivalue_line("markers", "docker_endpoints(*names): Docker endpoints a contract case uses")


@pytest.fixture(params=["fake", "real"])
async def backend(request):
    if request.param == "real":
        if not REAL:
            pytest.skip("real Docker: set SLOTSERVE_TEST_DOCKER=1 (CI job contract-docker)")
        docker = DockerClient(os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock")
        b = Backend("real", docker, None)
    else:
        fake = FakeDocker()
        docker = DockerClient("unix:///fake/docker.sock", transport=httpx.ASGITransport(app=fake.app))
        b = Backend("fake", docker, fake)
    if not await docker.image_exists(IMAGE):
        await docker.pull(IMAGE)
    start = len(b.fake.used) if b.fake is not None else 0
    try:
        yield b
    finally:
        try:
            await b.cleanup()
        finally:
            await docker.close()
    if b.fake is not None:
        # fake mode: the case used only the endpoints it declares, and the fake refused nothing
        declared = {name for mark in request.node.iter_markers("docker_endpoints") for name in mark.args}
        used = set(b.fake.used[start:])
        assert used <= declared, f"undeclared endpoints used: {sorted(used - declared)}"
        assert not b.fake.rejected, f"the fake refused: {b.fake.rejected}"

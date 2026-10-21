"""Smoke test of the main user journeys through the real UI."""

import json
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect

from conftest import INVITE, PASSWORD, sign_in


def _api(page: Page, base: str, method: str, path: str, data=None):
    resp = page.request.fetch(f"{base}{path}", method=method, data=data)
    assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
    return resp.json() if resp.status != 204 else None


def test_first_user_registers_and_sets_up_models(page: Page, server: str):
    page.goto(f"{server}/login")
    page.get_by_role("tab", name="Sign up").click()
    inputs = page.locator("input")
    inputs.nth(0).fill("admin")
    inputs.nth(1).fill(PASSWORD)
    inputs.nth(2).fill(INVITE)
    page.get_by_role("button", name="Sign up", exact=True).last.click()
    page.wait_for_url(f"{server}/")
    expect(page.get_by_role("link", name="Admin", exact=True)).to_be_visible()

    page.get_by_role("link", name="Models", exact=True).click()
    page.get_by_role("button", name="Scan folder").click()
    expect(page.get_by_text("chat-small", exact=True)).to_be_visible()
    models = {m["name"]: m["id"] for m in _api(page, server, "GET", "/api/models")}
    _api(page, server, "POST", f"/api/models/{models['chat-small']}/default")
    assert not page.errors  # type: ignore[attr-defined]


def test_api_key_and_gateway(page: Page, server: str):
    sign_in(page, server, "admin")
    page.get_by_role("link", name="Access credentials", exact=True).click()
    page.get_by_role("button", name="New key").click()
    page.get_by_role("dialog").locator("input").fill("e2e")
    page.get_by_role("dialog").get_by_role("button", name="Create").click()
    key = page.get_by_role("dialog").locator("code").first.inner_text().strip()
    assert key.startswith("sk-ss-")
    page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).last.click()

    page.get_by_role("link", name="Overview", exact=True).click()
    expect(page.locator(".model-card").get_by_text("Ready", exact=True)).to_be_visible(timeout=30_000)
    req = urllib.request.Request(
        f"{server}/v1/chat/completions",
        data=json.dumps({"messages": [{"role": "user", "content": "hello e2e"}]}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    assert "hello e2e" in body["choices"][0]["message"]["content"]


def test_book_and_cancel(page: Page, server: str):
    sign_in(page, server, "admin")
    page.get_by_role("link", name="Schedule", exact=True).click()
    page.get_by_role("button", name="New booking").first.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_text("Whole GPU", exact=True).click()
    dialog.get_by_role("button", name="New booking").click()
    expect(dialog).to_be_hidden()
    mine = page.locator(".mine li").first
    expect(mine).to_contain_text("Whole GPU")
    mine.get_by_role("button", name="Cancel booking").click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm").click()
    expect(page.locator(".mine li")).to_have_count(0)
    assert not page.errors  # type: ignore[attr-defined]


def test_admin_pages_render(page: Page, server: str):
    sign_in(page, server, "admin")
    for tab in (
        "overview",
        "users",
        "reservations",
        "requests",
        "keys",
        "channels",
        "storage",
        "settings",
        "audit",
    ):
        page.goto(f"{server}/admin?tab={tab}")
        expect(page.locator(".n-tabs-tab--active")).to_be_visible()
        page.wait_for_load_state("networkidle")
    page.goto(f"{server}/settings")
    page.get_by_text("Dark", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert not page.errors  # type: ignore[attr-defined]


def test_lanes_and_approval_ui(page: Page, server: str):
    sign_in(page, server, "admin")
    models = {m["name"]: m["id"] for m in _api(page, server, "GET", "/api/models")}
    _api(
        page,
        server,
        "POST",
        "/api/admin/lanes",
        {"name": "Second", "gpus": [], "default_model_id": models["chat-large"]},
    )
    page.goto(f"{server}/")
    expect(page.locator(".top.multi .lane")).to_have_count(2)
    page.goto(f"{server}/schedule")
    expect(page.locator(".lane-tab")).to_have_count(2)
    page.locator(".lane-tab", has_text="Second").click()
    page.goto(f"{server}/admin?tab=lanes")
    expect(page.get_by_text("Second", exact=True).first).to_be_visible()

    # approval mode: a member's booking waits for review
    _api(page, server, "PUT", "/api/admin/settings", {"approval_mode": "all"})
    member = page.context.browser.new_context(locale="en-US").new_page()
    member.request.post(
        f"{server}/api/auth/register", data={"username": "member", "password": PASSWORD, "invite_code": INVITE}
    )
    member.goto(f"{server}/schedule")
    member.get_by_role("button", name="New booking").first.click()
    dialog = member.get_by_role("dialog")
    expect(dialog.get_by_text("needs an admin's approval")).to_be_visible()
    dialog.get_by_role("button", name="New booking").click()
    expect(member.locator(".mine li").first).to_contain_text("Awaiting approval")
    member.context.close()

    page.goto(f"{server}/admin?tab=reservations")
    page.locator(".n-radio-button", has_text="Awaiting approval").click()
    page.get_by_role("button", name="Approve").first.click()
    expect(page.get_by_text("No bookings match these filters.")).to_be_visible()
    _api(page, server, "PUT", "/api/admin/settings", {"approval_mode": None})
    assert not page.errors  # type: ignore[attr-defined]


def test_usage_page(page: Page, server: str):
    sign_in(page, server, "admin")
    page.get_by_role("link", name="Usage", exact=True).click()
    expect(page.get_by_text("GPU-hours booked", exact=True)).to_be_visible()
    expect(page.get_by_text("GPU-hours over time", exact=True)).to_be_visible()
    page.get_by_text("By kind (recorded)", exact=True).click()
    page.get_by_text("Just me", exact=True).click()
    page.wait_for_load_state("networkidle")
    expect(page.get_by_text("During your bookings", exact=True)).to_be_visible()


def test_chat_channel_linking_ui(page: Page, server: str):
    sign_in(page, server, "admin")
    # An unreachable address: the bot cannot connect, but bind codes are still handed out.
    feishu = {"feishu_enabled": True, "feishu_app_id": "cli_e2e", "feishu_app_secret": "x"}
    _api(page, server, "PUT", "/api/admin/settings", {**feishu, "feishu_domain": "http://127.0.0.1:9"})
    page.goto(f"{server}/settings")
    row = page.locator(".ch-row[data-channel='feishu']")
    expect(row).to_contain_text("Feishu / Lark")
    row.get_by_role("button", name="Link").click()
    expect(row.locator(".code-box")).to_contain_text("bind ")
    page.goto(f"{server}/admin?tab=channels")
    expect(page.locator("tr[data-channel='feishu']")).to_contain_text("Feishu / Lark")
    _api(page, server, "PUT", "/api/admin/settings", {k: None for k in [*feishu, "feishu_domain"]})
    assert not page.errors  # type: ignore[attr-defined]


def test_access_credentials_and_idle_lane(page: Page, server: str):
    """SSH keys and the environment password live on the Access credentials page (with dev environments on)."""
    sign_in(page, server, "admin")
    page.goto(f"{server}/keys")
    expect(page.get_by_role("heading", name="API keys")).to_be_visible()
    expect(page.get_by_role("heading", name="Environment password")).to_have_count(0)
    _api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": True})
    try:
        page.goto(f"{server}/keys")
        expect(page.get_by_role("heading", name="SSH public keys")).to_be_visible()
        expect(page.get_by_text("No password set.")).to_be_visible()
        page.get_by_role("button", name="Set password").click()
        dialog = page.get_by_role("dialog")
        dialog.locator("input[type=password]").nth(0).fill("e2e environment pw")
        dialog.locator("input[type=password]").nth(1).fill("e2e environment pw")
        dialog.get_by_role("button", name="Save").click()
        expect(page.get_by_text("A password is set.")).to_be_visible()
        assert _api(page, server, "GET", "/api/devenv/password")["set"] is True

        # the dev environments page points to the credentials page instead of managing keys itself
        page.goto(f"{server}/devenv")
        expect(page.get_by_role("link", name="Access credentials").last).to_be_visible()
        expect(page.get_by_role("heading", name="SSH keys")).to_have_count(0)

        # a lane can run a shared environment while it is not booked
        page.goto(f"{server}/admin?tab=lanes")
        page.locator(".lanes").get_by_role("button", name="Edit").first.click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("When not booked")).to_be_visible()
        dialog.locator("[data-field=idle-mode]").click()
        page.get_by_text("Shared dev environment", exact=True).last.click()
        expect(dialog.locator("[data-field=idle-template]")).to_be_visible()
        dialog.get_by_role("button", name="Cancel").click()
    finally:
        _api(page, server, "DELETE", "/api/devenv/password")
        _api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": None})
    assert not page.errors  # type: ignore[attr-defined]


def test_storage_tab(page: Page, server: str):
    """Admin -> Storage: add a host directory, see its check result, remove it."""
    sign_in(page, server, "admin")
    page.goto(f"{server}/admin?tab=storage")
    expect(page.get_by_text("No storage yet", exact=False)).to_be_visible()
    page.locator("[data-test=storage-add]").click()
    dialog = page.get_by_role("dialog")
    dialog.locator("[data-field=storage-name] input").fill("E2E data")
    dialog.locator("[data-field=storage-host-path] input").fill("/srv/e2e-data")
    dialog.locator("[data-field=storage-mount] input").fill("/data/e2e")
    dialog.locator("[data-test=storage-save]").click()
    expect(dialog).to_be_hidden()
    row = page.locator(".storages tr", has_text="E2E data")
    expect(row).to_contain_text("/srv/e2e-data")
    expect(row).to_contain_text("/data/e2e")
    # no Docker here: the check that runs after saving reports the storage as not reachable
    expect(row.locator("[data-test=storage-state]")).to_have_text("Not reachable", timeout=30_000)
    [storage] = _api(page, server, "GET", "/api/admin/storage")
    assert storage["name"] == "E2E data" and storage["status"]["ok"] is False

    row.get_by_role("button", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete").click()
    expect(page.get_by_text("No storage yet", exact=False)).to_be_visible()


def test_devenv_without_gpu_ui(page: Page, server: str):
    """Dev environments without a GPU: the section on the dev environments page and the admin settings."""
    sign_in(page, server, "admin")
    _api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": True})
    try:
        info = _api(page, server, "GET", "/api/devenv/cpu")
        assert info["enabled"] is True and info["per_user"] == 2 and info["sessions"] == []
        page.goto(f"{server}/devenv")
        panel = page.locator("[data-test=cpu-panel]")
        expect(panel.get_by_role("heading", name="Without a GPU")).to_be_visible()
        expect(panel.get_by_text("0 of 2 running for you")).to_be_visible()
        expect(panel.get_by_role("button", name="Start without GPU")).to_be_visible()
        expect(page.locator("[data-test=cpu-start]").first).to_be_visible()

        page.goto(f"{server}/admin?tab=settings")
        expect(page.get_by_text("CPU cores reserved per GPU")).to_be_visible()
        expect(page.get_by_text("Without-GPU environments per person")).to_be_visible()
    finally:
        _api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": None})


def test_model_catalog_deploy(page: Page, server: str):
    sign_in(page, server, "admin")
    # Put a catalog file into the models folder so deploying needs no download.
    small = next(m for m in _api(page, server, "GET", "/api/models") if m["name"] == "chat-small")
    (Path(small["file_path"]).parent / "nomic-embed-text-v1.5.Q8_0.gguf").write_bytes(b"GGUF" + b"\0" * 1024)

    page.goto(f"{server}/models")
    page.locator(".view-tabs .n-tabs-tab", has_text="Catalog").click()
    expect(page.locator(".cat-card").first).to_be_visible()
    page.get_by_role("tab", name="Embeddings").click()
    page.get_by_placeholder("Search models").fill("nomic")
    card = page.locator(".cat-card", has_text="Nomic Embed Text v1.5")
    expect(page.locator(".cat-card")).to_have_count(1)
    card.get_by_role("button", name="Details").click()
    drawer = page.locator(".n-drawer")
    expect(drawer.get_by_text("Downloaded").first).to_be_visible()
    drawer.get_by_role("button", name="Deploy").first.click()
    dialog = page.get_by_role("dialog").last
    expect(dialog.get_by_text("Estimated GPU memory")).to_be_visible()
    dialog.get_by_role("button", name="Deploy").click()
    expect(page.get_by_text("nomic-embed-text-v1.5-q8_0 is ready")).to_be_visible()
    model = next(m for m in _api(page, server, "GET", "/api/models") if m["name"] == "nomic-embed-text-v1.5-q8_0")
    assert model["args"] == "-ngl 99 -c 8192 --embedding --pooling mean"
    assert not page.errors  # type: ignore[attr-defined]

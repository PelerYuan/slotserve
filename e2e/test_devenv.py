"""Dev environments: access credentials, idle lanes, environments without a GPU."""

from playwright.sync_api import Page, expect

from helpers import api, sign_in


def test_access_credentials_and_idle_lane(page: Page, server: str):
    """SSH keys and the environment password live on the Access credentials page (with dev environments on)."""
    sign_in(page, server, "admin")
    page.goto(f"{server}/keys")
    expect(page.get_by_role("heading", name="API keys")).to_be_visible()
    expect(page.get_by_role("heading", name="Environment password")).to_have_count(0)
    api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": True})
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
        assert api(page, server, "GET", "/api/devenv/password")["set"] is True

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
        api(page, server, "DELETE", "/api/devenv/password")
        api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": None})
    assert not page.errors  # type: ignore[attr-defined]


def test_devenv_without_gpu_ui(page: Page, server: str):
    """Dev environments without a GPU: the section on the dev environments page and the admin settings."""
    sign_in(page, server, "admin")
    api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": True})
    try:
        info = api(page, server, "GET", "/api/devenv/cpu")
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
        api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": None})

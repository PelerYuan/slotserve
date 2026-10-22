"""Admin pages: every tab renders; Admin -> Storage."""

from playwright.sync_api import Page, expect

from helpers import api, sign_in


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
    [storage] = api(page, server, "GET", "/api/admin/storage")
    assert storage["name"] == "E2E data" and storage["status"]["ok"] is False

    row.get_by_role("button", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete").click()
    expect(page.get_by_text("No storage yet", exact=False)).to_be_visible()

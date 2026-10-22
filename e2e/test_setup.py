"""First run: the first person to sign up becomes the admin and sets up the models."""

from playwright.sync_api import Page, expect

from helpers import INVITE, PASSWORD, api


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
    models = {m["name"]: m["id"] for m in api(page, server, "GET", "/api/models")}
    api(page, server, "POST", f"/api/models/{models['chat-small']}/default")
    assert not page.errors  # type: ignore[attr-defined]

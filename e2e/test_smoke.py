"""Smoke test of the main user journeys through the real UI."""

import json
import urllib.request

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
    page.get_by_role("link", name="API keys", exact=True).click()
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

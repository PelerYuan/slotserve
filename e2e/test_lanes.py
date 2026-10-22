"""Several lanes and the approval of bookings."""

from playwright.sync_api import Page, expect

from helpers import INVITE, PASSWORD, api, sign_in


def test_lanes_and_approval_ui(page: Page, server: str):
    sign_in(page, server, "admin")
    models = {m["name"]: m["id"] for m in api(page, server, "GET", "/api/models")}
    api(
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
    api(page, server, "PUT", "/api/admin/settings", {"approval_mode": "all"})
    member = page.context.browser.new_context(locale="en-US").new_page()
    member.request.post(
        f"{server}/api/auth/register", data={"username": "member", "password": PASSWORD, "invite_code": INVITE}
    )
    member.goto(f"{server}/schedule")
    member.get_by_role("button", name="New booking").first.click()
    dialog = member.get_by_role("dialog")  # QuickBook
    expect(dialog.get_by_text("needs an admin's approval")).to_be_visible()
    dialog.locator("[data-action=quickbook-submit]").click()
    expect(member.locator(".mine li").first).to_contain_text("Awaiting approval")
    member.context.close()

    page.goto(f"{server}/admin?tab=reservations")
    page.locator(".n-radio-button", has_text="Awaiting approval").click()
    page.get_by_role("button", name="Approve").first.click()
    expect(page.get_by_text("No bookings match these filters.")).to_be_visible()
    api(page, server, "PUT", "/api/admin/settings", {"approval_mode": None})
    assert not page.errors  # type: ignore[attr-defined]

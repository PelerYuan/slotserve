"""The bell (F01): everyone has an inbox; admins also get the admin messages and a "waiting for you" line above the
Overview's slot. Works without any chat platform."""

from datetime import UTC, datetime, timedelta

from playwright.sync_api import Browser, Page, expect

from helpers import INVITE, PASSWORD, api, sign_in

H = {"X-SlotServe": "1"}


def test_approval_request_and_answer_reach_both_inboxes(browser: Browser, page: Page, server: str):
    sign_in(page, server, "admin")
    member = browser.new_context(viewport={"width": 1366, "height": 860}, locale="en-US").new_page()
    r = member.request.post(
        f"{server}/api/auth/register",
        data={"username": "inbox-member", "password": PASSWORD, "invite_code": INVITE},
        headers=H,
    )
    assert r.ok, r.text()
    api(page, server, "POST", "/api/me/notifications/read", {"all": True})
    api(page, server, "PUT", "/api/admin/settings", {"approval_mode": "all"})
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
    booking = None
    try:
        booking = api(
            member,
            server,
            "POST",
            "/api/reservations",
            {"model_id": None, "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()},
        )[0]
        assert booking["status"] == "pending"

        # the admin: the bell, the Admin badge, and one line above the slot (it does not take the slot)
        page.goto(f"{server}/")
        expect(page.locator("[data-field=needs-action]")).to_contain_text("to approve", timeout=15_000)
        expect(page.locator("aside [data-field=inbox-count]")).to_be_visible(timeout=15_000)
        expect(page.locator("aside [data-field=admin-inbox-count]")).to_be_visible()
        page.locator("aside [data-action=inbox-bell]").click()
        inbox = page.locator("[data-component=inbox]")
        expect(inbox.locator("[data-event=approval_requested]").first).to_contain_text("inbox-member")
        inbox.locator(".n-tabs-tab", has_text="Admin").click()
        expect(inbox.locator("[data-event=approval_requested]").first).to_be_visible()
        inbox.locator("[data-event=approval_requested]").first.click()  # read, and off to the bookings
        expect(page).to_have_url(f"{server}/admin?tab=reservations", timeout=10_000)

        api(page, server, "POST", f"/api/admin/reservations/{booking['id']}/approve", {"series": False})

        # the member: their own news only, no admin tab
        member.goto(f"{server}/")
        expect(member.locator("aside [data-field=inbox-count]")).to_be_visible(timeout=15_000)
        expect(member.locator("aside [data-field=admin-inbox-count]")).to_have_count(0)
        member.locator("aside [data-action=inbox-bell]").click()
        mine = member.locator("[data-component=inbox]")
        expect(mine.locator("[data-event=booking_approved]")).to_be_visible()
        expect(mine.locator("[data-event=approval_requested]")).to_have_count(0)
        expect(mine.locator(".n-tabs-tab")).to_have_count(0)
        mine.locator("[data-action=inbox-read-all]").click()
        expect(member.locator("aside [data-field=inbox-count]")).to_have_count(0)
        assert api(member, server, "GET", "/api/me/notifications/unread")["total"] == 0
    finally:
        if booking:
            member.request.fetch(f"{server}/api/reservations/{booking['id']}", method="DELETE", headers=H)
        api(page, server, "PUT", "/api/admin/settings", {"approval_mode": None})
        member.context.close()
    assert not page.errors  # type: ignore[attr-defined]


def test_mobile_header_has_the_bell(browser: Browser, server: str):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="en-US")
    page = ctx.new_page()
    page.request.post(f"{server}/api/auth/login", data={"username": "admin", "password": PASSWORD}, headers=H)
    page.goto(f"{server}/")
    page.locator("header [data-action=inbox-bell]").click()
    expect(page.locator("[data-component=inbox]")).to_be_visible()
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390  # no sideways scrolling
    ctx.close()

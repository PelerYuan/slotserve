"""The model availability badge (F05): one answer from the server, the same on the Overview, Models and in the
Playground."""

from datetime import UTC, datetime, timedelta

from playwright.sync_api import Browser, Page, expect

from helpers import INVITE, PASSWORD, api, sign_in

H = {"X-SlotServe": "1"}


def _states(page: Page, server: str) -> dict[str, dict]:
    return {m["name"]: m for m in api(page, server, "GET", "/api/status")["models"]}


def test_badge_agrees_everywhere_and_follows_a_booking(browser: Browser, page: Page, server: str):
    sign_in(page, server, "admin")
    member = browser.new_context(viewport={"width": 1366, "height": 860}, locale="en-US").new_page()
    r = member.request.post(
        f"{server}/api/auth/register",
        data={"username": "badge-member", "password": PASSWORD, "invite_code": INVITE},
        headers=H,
    )
    assert r.ok, r.text()

    states = _states(member, server)
    name, now_model = next((n, m) for n, m in states.items() if m["state"] == "now" and not m["loading"])

    def badge_everywhere(state: str):
        member.goto(f"{server}/models")
        row = member.locator("ul.models li", has_text=name).first
        expect(row.locator(f"[data-availability={state}]")).to_be_visible(timeout=10_000)
        member.goto(f"{server}/")
        expect(member.locator(f"[data-availability={state}]").first).to_be_visible(timeout=10_000)
        member.locator("[data-action=open-playground]").click()
        drawer = member.locator("[data-component=playground]")
        drawer.locator("[data-field=playground-model]").click()
        option = member.locator(".n-base-select-option", has_text=name)
        expect(option.locator(f"[data-availability={state}]")).to_be_visible()
        member.keyboard.press("Escape")
        member.keyboard.press("Escape")

    badge_everywhere("now")

    # someone else books that GPU with that model: "in use by admin until ..." everywhere, and not offered
    now = datetime.now(UTC)
    booking = api(
        page,
        server,
        "POST",
        "/api/reservations",
        {
            "model_id": now_model["id"],
            "lane_id": now_model["lane_id"],
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )[0]
    try:
        busy = _states(member, server)[name]
        assert busy["state"] == "busy" and busy["holder"] == "admin"
        member.goto(f"{server}/models")
        row = member.locator("ul.models li", has_text=name).first
        expect(row.locator("[data-availability=busy]")).to_contain_text("In use by admin")
        expect(row.locator(f"[data-action=book-{name}]")).to_be_visible()
        member.goto(f"{server}/")
        member.locator("[data-action=open-playground]").click()
        member.locator("[data-field=playground-model]").click()
        option = member.locator(".n-base-select-option", has_text=name)
        expect(option.locator("[data-availability=busy]")).to_be_visible()
        assert "n-base-select-option--disabled" in (option.get_attribute("class") or "")
        # for the person holding the booking it stays callable
        assert _states(page, server)[name]["state"] == "now"
    finally:
        api(page, server, "DELETE", f"/api/reservations/{booking['id']}")
        member.context.close()
    assert not page.errors  # type: ignore[attr-defined]

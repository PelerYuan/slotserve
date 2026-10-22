"""Newcomers (F01, F05): "start here", the Playground without a key, "copy code" with a key made on request, the
one-time suggestion, and the Overview's single next-step slot."""

import re
from datetime import UTC, datetime, timedelta

from playwright.sync_api import Browser, Page, expect

from helpers import INVITE, PASSWORD, api, sign_in

H = {"X-SlotServe": "1"}
DONE = re.compile(r"(^|\s)done(\s|$)")


def _newcomer(browser: Browser, server: str, name: str, **ctx) -> Page:
    """A new account, signed in (in a context of its own)."""
    context = browser.new_context(viewport={"width": 1366, "height": 860}, locale="en-US", **ctx)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    r = page.request.post(
        f"{server}/api/auth/register", data={"username": name, "password": PASSWORD, "invite_code": INVITE}, headers=H
    )
    assert r.ok, r.text()
    return page


def _slot_cards(page: Page):
    return page.locator("[data-component=next-step] section.card")


def test_newcomer_calls_a_model_without_a_key_then_copies_code(browser: Browser, page: Page, server: str):
    sign_in(page, server, "admin")
    newbie = _newcomer(browser, server, "newcomer1", permissions=["clipboard-read", "clipboard-write"])
    newbie.goto(f"{server}/")
    expect(newbie.locator("[data-card=start]")).to_be_visible()
    expect(_slot_cards(newbie)).to_have_count(1)  # one card at most
    expect(newbie.locator("[data-goal=call]")).not_to_have_class(DONE)

    newbie.locator("[data-action=start-call]").click()
    drawer = newbie.locator("[data-component=playground]")
    expect(drawer).to_be_visible()
    expect(drawer.locator("[data-field=playground-model]")).to_contain_text("Callable now")
    drawer.locator("[data-field=playground-input] textarea").fill("hello from the playground")
    drawer.locator("[data-action=playground-send]").click()
    expect(drawer.locator(".msg.assistant").first).to_contain_text("hello from the playground", timeout=10_000)
    assert newbie.url.rstrip("/") == server  # no jump to another page
    assert api(newbie, server, "GET", "/api/keys") == []  # and no key

    # metered as the person, from the playground
    log = api(page, server, "GET", "/api/admin/requests?source=playground&limit=50")
    assert any(i["username"] == "newcomer1" for i in log["items"])

    # the one-time suggestion after the first answer
    suggestion = drawer.locator("[data-suggestion=playground_first_reply]")
    expect(suggestion).to_be_visible()
    suggestion.get_by_role("button", name="Don’t show again").click()
    expect(suggestion).to_be_hidden()

    # "copy code": a key named playground (90 days) is made on request and filled into the code
    drawer.locator("[data-action=playground-copy-code]").click()
    newbie.get_by_role("button", name="Create key and copy").click()
    code = drawer.locator("[data-field=playground-code]")
    expect(code).to_contain_text("sk-ss-")
    expect(code).not_to_contain_text("sk-ss-...")
    keys = api(newbie, server, "GET", "/api/keys")
    assert [k["name"] for k in keys] == ["playground"] and keys[0]["scope"] == "llm"
    expires = datetime.fromisoformat(keys[0]["expires_at"].replace("Z", "+00:00"))
    assert timedelta(days=89) < expires - datetime.now(UTC) <= timedelta(days=90)
    copied = newbie.evaluate("navigator.clipboard.readText()")
    assert keys[0]["prefix"] in copied and f"{server}/v1" in copied
    stored = newbie.evaluate("JSON.stringify(Object.assign({}, localStorage))")
    assert "sk-ss-" not in stored  # the key lives only on the page

    # the call goal is ticked from the real request; the suggestion never comes back
    newbie.keyboard.press("Escape")
    newbie.reload()
    expect(newbie.locator("[data-goal=call]")).to_have_class(DONE)
    newbie.locator("[data-action=open-playground]").click()
    drawer.locator("[data-field=playground-input] textarea").fill("again")
    drawer.locator("[data-action=playground-send]").click()
    expect(drawer.locator(".msg.assistant").first).to_contain_text("again", timeout=10_000)
    expect(drawer.locator("[data-suggestion]")).to_have_count(0)
    newbie.keyboard.press("Escape")

    # "don't show again" holds on another device
    newbie.locator("[data-action=start-dismiss]").click()
    expect(newbie.locator("[data-card=start]")).to_have_count(0)
    other = browser.new_context(locale="en-US").new_page()
    other.request.post(f"{server}/api/auth/login", data={"username": "newcomer1", "password": PASSWORD}, headers=H)
    other.goto(f"{server}/")
    expect(other.get_by_role("heading", name="Overview")).to_be_visible()
    expect(other.locator("[data-card=start]")).to_have_count(0)
    other.context.close()
    assert not newbie.errors  # type: ignore[attr-defined]
    newbie.context.close()


def test_playground_switched_off_or_rate_limited_explains_itself(browser: Browser, page: Page, server: str):
    sign_in(page, server, "admin")
    newbie = _newcomer(browser, server, "newcomer2")
    try:
        api(page, server, "PUT", "/api/admin/settings", {"playground_rate_per_minute": 1})
        newbie.goto(f"{server}/")
        newbie.locator("[data-action=open-playground]").click()
        drawer = newbie.locator("[data-component=playground]")
        box = drawer.locator("[data-field=playground-input] textarea")
        box.fill("one")
        drawer.locator("[data-action=playground-send]").click()
        expect(drawer.locator(".msg.assistant").first).to_be_visible(timeout=10_000)
        box.fill("two")
        drawer.locator("[data-action=playground-send]").click()
        expect(drawer.locator("[data-error=playground_rate_limited]")).to_contain_text("limit of requests per minute")
        expect(drawer.locator("[data-error=playground_rate_limited]")).to_contain_text("Try again in")

        api(page, server, "PUT", "/api/admin/settings", {"playground_enabled": False})
        newbie.reload()
        newbie.locator("[data-action=open-playground]").click()
        expect(drawer.locator("[data-field=playground-disabled]")).to_be_visible()
        expect(drawer.locator("[data-action=playground-send]")).to_have_count(0)
        expect(drawer.locator("[data-action=playground-copy-code]")).to_be_visible()
        assert not newbie.errors  # type: ignore[attr-defined]
    finally:
        api(
            page, server, "PUT", "/api/admin/settings", {"playground_enabled": None, "playground_rate_per_minute": None}
        )
        newbie.context.close()


def test_my_work_takes_the_slot_and_plus_one_hour_extends(browser: Browser, page: Page, server: str):
    newbie = _newcomer(browser, server, "newcomer3")
    status = api(newbie, server, "GET", "/api/status")
    free = next(lane for lane in status["lanes"] if lane["active_reservation"] is None)
    now = datetime.now(UTC)
    booking = api(
        newbie,
        server,
        "POST",
        "/api/reservations",
        {
            "model_id": None,
            "lane_id": free["id"],
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )[0]
    try:
        newbie.goto(f"{server}/")
        work = newbie.locator("[data-card=work]")
        expect(work).to_be_visible()
        expect(_slot_cards(newbie)).to_have_count(1)  # "start here" folds into a link
        expect(newbie.locator("[data-fold=start]")).to_be_visible()
        expect(work).to_contain_text("Whole GPU")
        expect(work).to_contain_text("left")
        work.locator("[data-action=work-extend]").click()
        expect(newbie.get_by_text("Extended by an hour.")).to_be_visible()
        after = next(r for r in api(newbie, server, "GET", "/api/reservations?mine=true") if r["id"] == booking["id"])
        assert after["end_at"] > booking["end_at"]

        # the exclusive booking shows as in use, not as a stopped model
        lane_card = (
            newbie.locator(".lane", has_text=free["name"])
            if len(status["lanes"]) > 1
            else newbie.locator(".model-card")
        )
        expect(lane_card.locator(".in-use")).to_be_visible()
        expect(lane_card).not_to_contain_text("Stopped")

        newbie.locator("[data-fold=start]").click()
        expect(newbie.locator("[data-card=start]")).to_be_visible()
        expect(_slot_cards(newbie)).to_have_count(1)
        assert not newbie.errors  # type: ignore[attr-defined]
    finally:
        api(newbie, server, "DELETE", f"/api/reservations/{booking['id']}")
        newbie.context.close()

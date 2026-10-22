"""W1-07: invite links from the admin UI to the sign-up page, approvals, the global password prompt of sudo mode and
the signed-in devices list."""

from playwright.sync_api import Browser, Page, expect

from helpers import INVITE, PASSWORD, api, sign_in


def _open(browser: Browser, url: str):
    ctx = browser.new_context(viewport={"width": 1366, "height": 860}, locale="en-US")
    page = ctx.new_page()
    page.goto(url)
    return ctx, page


def test_invite_link_signs_up_and_can_be_revoked(page: Page, server: str, browser: Browser):
    sign_in(page, server, "admin")
    page.goto(f"{server}/admin?tab=users")
    invites = page.locator("[data-testid=invites]")
    invites.get_by_role("button", name="New invite link").click()
    dialog = page.locator("[data-testid=invite-dialog]")
    dialog.locator("input").first.fill("e2e course")
    dialog.get_by_role("button", name="Create").click()
    url = dialog.locator("[data-testid=invite-url]").inner_text().strip()
    assert "/login?invite=ssi-" in url and INVITE not in url
    message = dialog.locator("[data-testid=invite-message]").inner_text()
    assert url in message and "dev environment" in message
    dialog.get_by_role("radio", name="中文").click(force=True)
    expect(dialog.locator("[data-testid=invite-message]")).to_contain_text("注册后你可以")
    dialog.get_by_role("button", name="Close", exact=True).click()

    ctx, guest = _open(browser, url)
    try:
        expect(guest.locator("[data-testid=invite-note]")).to_be_visible()
        guest.locator("input").nth(0).fill("w107-invited")
        guest.locator("input").nth(1).fill(PASSWORD)
        guest.get_by_role("button", name="Sign up", exact=True).last.click()
        guest.wait_for_url(f"{server}/")
    finally:
        ctx.close()

    page.reload()
    row = page.locator("[data-testid=invites] tr", has_text="e2e course")
    expect(row).to_contain_text("w107-invited")
    expect(row).to_contain_text("1 / 50")
    row.get_by_role("button", name="Revoke").click()
    page.get_by_role("dialog").get_by_role("button", name="Revoke").click()
    expect(page.locator("[data-testid=invites] tr", has_text="e2e course")).to_have_count(0)  # inactive are folded

    ctx, guest = _open(browser, url)
    try:
        expect(guest.get_by_text("This invite link was revoked", exact=False)).to_be_visible()
    finally:
        ctx.close()
    assert not page.errors  # type: ignore[attr-defined]


def test_invite_with_approval_waits_for_an_admin(page: Page, server: str, browser: Browser):
    sign_in(page, server, "admin")
    created = api(page, server, "POST", "/api/admin/invites", {"note": "needs ok", "requires_approval": True})
    ctx, guest = _open(browser, f"{server}{created['path']}")
    try:
        expect(guest.locator("[data-testid=invite-note]")).to_contain_text("approves")
        guest.locator("input").nth(0).fill("w107-waiting")
        guest.locator("input").nth(1).fill(PASSWORD)
        guest.get_by_role("button", name="Sign up", exact=True).last.click()
        expect(guest.locator(".error")).to_contain_text("approve")
        assert guest.url.startswith(f"{server}/login")
    finally:
        ctx.close()

    page.goto(f"{server}/admin?tab=users")
    pending = page.locator("[data-testid=pending-approvals]")
    item = pending.locator("li", has_text="w107-waiting")
    expect(item).to_contain_text("needs ok")
    item.get_by_role("button", name="Approve").click()
    expect(pending.locator("li", has_text="w107-waiting")).to_have_count(0)
    api(page, server, "POST", "/api/auth/logout")
    sign_in(page, server, "w107-waiting")  # can sign in now
    assert not page.errors  # type: ignore[attr-defined]


def test_sudo_prompt_asks_for_the_password_and_repeats_the_save(page: Page, server: str, browser: Browser):
    # an admin who has not entered the password in this session: signed up as a member, then made an admin
    ctx = browser.new_context(viewport={"width": 1366, "height": 860}, locale="en-US")
    r = ctx.request.post(
        f"{server}/api/auth/register",
        data={"username": "w107-root", "password": PASSWORD, "invite_code": INVITE},
        headers={"X-SlotServe": "1"},
    )
    assert r.ok, r.text()
    sign_in(page, server, "admin")
    uid = next(u["id"] for u in api(page, server, "GET", "/api/admin/users") if u["username"] == "w107-root")
    api(page, server, "PATCH", f"/api/admin/users/{uid}", {"is_admin": True})
    assert not ctx.request.get(f"{server}/api/auth/sudo").json()["active"]

    other = ctx.new_page()
    errors: list[str] = []
    other.on("pageerror", lambda e: errors.append(str(e)))
    try:
        other.goto(f"{server}/admin?tab=settings&q=custom_engine_cmd")
        row = other.locator("[data-group] [data-setting=custom_engine_cmd]")
        expect(row.locator("[data-danger]")).to_be_visible()
        row.locator("input").fill("my-engine --port {port}")
        other.locator("[data-save=engines]").click()
        preview = other.locator("[data-testid=settings-preview]")
        expect(preview).to_contain_text("ask for your password")
        preview.locator("[data-testid=preview-confirm]").click()

        prompt = other.locator(".sudo-modal")
        expect(prompt).to_be_visible()
        expect(prompt).to_contain_text("Confirm your password")
        prompt.locator("input[type=password]").fill("wrong-password")
        prompt.get_by_role("button", name="Confirm").click()
        expect(prompt).to_contain_text("Wrong")
        prompt.locator("input[type=password]").fill(PASSWORD)
        prompt.get_by_role("button", name="Confirm").click()
        expect(prompt).to_be_hidden()
        expect(preview).to_be_hidden()
        values = {s["key"]: s["value"] for s in api(page, server, "GET", "/api/admin/settings")}
        assert values["custom_engine_cmd"] == "my-engine --port {port}"
        assert ctx.request.get(f"{server}/api/auth/sudo").json()["active"]
    finally:
        api(page, server, "PUT", "/api/admin/settings", {"custom_engine_cmd": None})
        api(page, server, "PATCH", f"/api/admin/users/{uid}", {"is_admin": False})
        ctx.close()
    assert not errors


def test_signed_in_devices(page: Page, server: str, browser: Browser):
    other = browser.new_context()
    r = other.request.post(
        f"{server}/api/auth/login",
        data={"username": "admin", "password": PASSWORD},
        headers={"X-SlotServe": "1"},
    )
    assert r.ok
    try:
        sign_in(page, server, "admin")
        page.goto(f"{server}/settings")
        devices = page.locator("[data-testid=devices]")
        expect(devices.locator("li[data-current=true]")).to_contain_text("This browser")
        expect(devices.locator("li")).not_to_have_count(1)
        devices.get_by_role("button", name="Sign out all other devices").click()
        page.get_by_role("dialog").get_by_role("button", name="Sign out all other devices").click()
        expect(devices.locator("li")).to_have_count(1)
        assert other.request.get(f"{server}/api/auth/me").status == 401
    finally:
        other.close()
    assert not page.errors  # type: ignore[attr-defined]

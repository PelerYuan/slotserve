"""Launch-argument policy in the UI: a member's request for an admin, the admin's confirmation and approval."""

from pathlib import Path

from playwright.sync_api import Page, expect

from helpers import INVITE, PASSWORD, api, sign_in

CSRF = {"X-SlotServe": "1"}  # what the frontend's API client sends (security/csrf.py)


def _register(page: Page, base: str, username: str) -> None:
    resp = page.request.post(
        f"{base}/api/auth/register",
        data={"username": username, "password": PASSWORD, "invite_code": INVITE},
        headers=CSRF,
    )
    assert resp.ok, resp.text()
    page.request.post(f"{base}/api/auth/logout", headers=CSRF)


def test_member_requests_and_admin_approves(page: Page, server: str):
    base = server
    sign_in(page, base, "admin")  # created by test_setup.py
    small = next(m for m in api(page, base, "GET", "/api/models") if m["name"] == "chat-small")
    models = Path(small["file_path"]).parent
    (models / "member-model.gguf").write_bytes(b"GGUF" + bytes(2048))
    page.request.post(f"{base}/api/auth/logout", headers=CSRF)
    _register(page, base, "args-member")

    sign_in(page, base, "args-member")
    page.goto(f"{base}/models")
    page.get_by_role("button", name="Add model").click()
    dialog = page.get_by_role("dialog")
    dialog.locator("input").nth(0).fill("member-model")
    dialog.get_by_placeholder("/models/Qwen3-8B-Q4_K_M.gguf").fill(str(models / "member-model.gguf"))
    dialog.locator("textarea").first.fill("-ngl 99 --log-file /tmp/x.log")
    dialog.get_by_role("button", name="Save").click()
    # the arguments need an admin: the form offers to send a request
    expect(page.get_by_text("before the model uses them: --log-file")).to_be_visible()
    page.get_by_role("button", name="Ask an admin").click()
    expect(page.get_by_text("Arguments awaiting approval")).to_be_visible()
    model = next(m for m in page.request.get(f"{base}/api/models").json() if m["name"] == "member-model")
    assert model["args_pending"] == "-ngl 99 --log-file /tmp/x.log"
    assert model["needs_approval"] == ["--log-file"]

    page.request.post(f"{base}/api/auth/logout", headers=CSRF)
    sign_in(page, base, "admin")
    page.goto(f"{base}/models")
    row = page.locator("li").filter(has_text="member-model")
    row.get_by_role("button").last.click()
    page.get_by_text("Approve requested arguments").click()
    # --log-file writes files: the admin confirms explicitly
    # wait for the approval itself: the badge also disappears for a moment while the list reloads
    # the first approve answers args_confirm_required (that is what shows "Use them"): wait for the confirmed one
    with page.expect_response(lambda r: "/args/approve" in r.url and r.ok) as approved:
        page.get_by_role("button", name="Use them").click()
    assert approved.value.ok, approved.value.text()
    expect(page.get_by_text("Arguments awaiting approval")).to_have_count(0)
    model = next(m for m in page.request.get(f"{base}/api/models").json() if m["name"] == "member-model")
    assert model["args"] == "-ngl 99 --log-file /tmp/x.log" and model["args_pending"] is None
    assert not page.errors  # type: ignore[attr-defined]

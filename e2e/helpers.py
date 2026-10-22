"""Shared constants and helpers of the end-to-end tests (fixtures stay in conftest.py)."""

from playwright.sync_api import Page

INVITE = "e2e-invite"
PASSWORD = "e2e-pass-123"


def sign_in(page: Page, base: str, username: str, password: str = PASSWORD) -> None:
    page.goto(f"{base}/login")
    page.get_by_role("tab", name="Sign in").click()
    page.locator("input").nth(0).fill(username)
    page.locator("input").nth(1).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).last.click()
    page.wait_for_url(f"{base}/")


def api(page: Page, base: str, method: str, path: str, data=None):
    """Call the SlotServe API with the page's session; fails the test on an error status."""
    # X-SlotServe: what the frontend's API client sends (CsrfGuard refuses changing requests without it)
    resp = page.request.fetch(f"{base}{path}", method=method, data=data, headers={"X-SlotServe": "1"})
    assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
    return resp.json() if resp.status != 204 else None

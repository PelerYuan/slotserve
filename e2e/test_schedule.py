"""Booking GPU time on the Schedule page."""

from playwright.sync_api import Page, expect

from helpers import sign_in


def test_book_and_cancel(page: Page, server: str):
    sign_in(page, server, "admin")
    page.get_by_role("link", name="Schedule", exact=True).click()
    page.get_by_role("button", name="New booking").first.click()
    dialog = page.get_by_role("dialog")  # QuickBook
    dialog.get_by_text("Whole GPU", exact=True).click()
    dialog.locator("[data-action=quickbook-submit]").click()
    expect(dialog).to_be_hidden()
    mine = page.locator(".mine li").first
    expect(mine).to_contain_text("Whole GPU")
    mine.get_by_role("button", name="End now").click()  # QuickBook books from now
    page.get_by_role("dialog").get_by_role("button", name="Confirm").click()
    expect(page.locator(".mine li")).to_have_count(0)
    assert not page.errors  # type: ignore[attr-defined]

"""The Usage page."""

from playwright.sync_api import Page, expect

from helpers import sign_in


def test_usage_page(page: Page, server: str):
    sign_in(page, server, "admin")
    page.get_by_role("link", name="Usage", exact=True).click()
    expect(page.get_by_text("GPU-hours booked", exact=True)).to_be_visible()
    expect(page.get_by_text("GPU-hours over time", exact=True)).to_be_visible()
    page.get_by_text("By kind (recorded)", exact=True).click()
    page.get_by_text("Just me", exact=True).click()
    page.wait_for_load_state("networkidle")
    expect(page.get_by_text("During your bookings", exact=True)).to_be_visible()

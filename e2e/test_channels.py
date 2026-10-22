"""Linking chat platforms."""

from playwright.sync_api import Page, expect

from helpers import api, sign_in


def test_chat_channel_linking_ui(page: Page, server: str):
    sign_in(page, server, "admin")
    # An unreachable address: the bot cannot connect, but bind codes are still handed out.
    feishu = {"feishu_enabled": True, "feishu_app_id": "cli_e2e", "feishu_app_secret": "x"}
    api(page, server, "PUT", "/api/admin/settings", {**feishu, "feishu_domain": "http://127.0.0.1:9"})
    page.goto(f"{server}/settings")
    row = page.locator(".ch-row[data-channel='feishu']")
    expect(row).to_contain_text("Feishu / Lark")
    row.get_by_role("button", name="Link").click()
    expect(row.locator(".code-box")).to_contain_text("bind ")
    page.goto(f"{server}/admin?tab=channels")
    expect(page.locator("tr[data-channel='feishu']")).to_contain_text("Feishu / Lark")
    api(page, server, "PUT", "/api/admin/settings", {k: None for k in [*feishu, "feishu_domain"]})
    assert not page.errors  # type: ignore[attr-defined]

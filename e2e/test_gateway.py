"""API keys and the OpenAI-compatible gateway."""

import json
import urllib.request

from playwright.sync_api import Page, expect

from helpers import sign_in


def test_api_key_and_gateway(page: Page, server: str):
    sign_in(page, server, "admin")
    page.get_by_role("link", name="Access credentials", exact=True).click()
    page.get_by_role("button", name="New key").click()
    page.get_by_role("dialog").locator("input").fill("e2e")
    page.get_by_role("dialog").get_by_role("button", name="Create").click()
    key = page.get_by_role("dialog").locator("code").first.inner_text().strip()
    assert key.startswith("sk-ss-")
    page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).last.click()

    page.get_by_role("link", name="Overview", exact=True).click()
    expect(page.locator(".model-card").get_by_text("Ready", exact=True)).to_be_visible(timeout=30_000)
    req = urllib.request.Request(
        f"{server}/v1/chat/completions",
        data=json.dumps({"messages": [{"role": "user", "content": "hello e2e"}]}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    assert "hello e2e" in body["choices"][0]["message"]["content"]

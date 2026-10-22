"""Models: the catalog and one-click deploy."""

from pathlib import Path

from playwright.sync_api import Page, expect

from helpers import api, sign_in


def test_model_catalog_deploy(page: Page, server: str):
    sign_in(page, server, "admin")
    # Put a catalog file into the models folder so deploying needs no download.
    small = next(m for m in api(page, server, "GET", "/api/models") if m["name"] == "chat-small")
    (Path(small["file_path"]).parent / "nomic-embed-text-v1.5.Q8_0.gguf").write_bytes(b"GGUF" + b"\0" * 1024)

    page.goto(f"{server}/models")
    page.locator(".view-tabs .n-tabs-tab", has_text="Catalog").click()
    expect(page.locator(".cat-card").first).to_be_visible()
    page.get_by_role("tab", name="Embeddings").click()
    page.get_by_placeholder("Search models").fill("nomic")
    card = page.locator(".cat-card", has_text="Nomic Embed Text v1.5")
    expect(page.locator(".cat-card")).to_have_count(1)
    card.get_by_role("button", name="Details").click()
    drawer = page.locator(".n-drawer")
    expect(drawer.get_by_text("Downloaded").first).to_be_visible()
    drawer.get_by_role("button", name="Deploy").first.click()
    dialog = page.get_by_role("dialog").last
    expect(dialog.get_by_text("Estimated GPU memory")).to_be_visible()
    dialog.get_by_role("button", name="Deploy").click()
    expect(page.get_by_text("nomic-embed-text-v1.5-q8_0 is ready")).to_be_visible()
    model = next(m for m in api(page, server, "GET", "/api/models") if m["name"] == "nomic-embed-text-v1.5-q8_0")
    assert model["args"] == "-ngl 99 -c 8192 --embedding --pooling mean"
    assert not page.errors  # type: ignore[attr-defined]

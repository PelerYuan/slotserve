"""QuickBook (F02): what -> when -> where, with "where" chosen automatically by the server."""

from datetime import UTC, datetime, timedelta

from playwright.sync_api import Page, expect

from helpers import api, sign_in


def test_book_an_environment_in_two_clicks_on_a_free_lane(page: Page, server: str):
    sign_in(page, server, "admin")
    api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": True})
    api(page, server, "PUT", "/api/devenv/password", {"password": "e2e environment pw"})
    lane = api(page, server, "POST", "/api/admin/lanes", {"name": "QuickBook spare", "gpus": []})
    now = datetime.now(UTC)
    # every other lane is taken for the next three hours
    held = []
    for other in api(page, server, "GET", "/api/lanes"):
        if other["id"] != lane["id"]:
            body = {
                "model_id": None,
                "lane_id": other["id"],
                "start_at": now.isoformat(),
                "end_at": (now + timedelta(hours=3)).isoformat(),
            }
            resp = page.request.post(f"{server}/api/reservations", data=body, headers={"X-SlotServe": "1"})
            assert resp.ok or resp.status == 409, resp.text()  # 409: someone else already has it
            held += resp.json() if resp.ok else []
    created = []
    try:
        page.goto(f"{server}/devenv")
        page.get_by_role("button", name="Book an environment").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("Book a dev environment", exact=True)).to_be_visible()
        # the other lanes are taken, so the automatic choice is the free one; nothing to pick by hand
        expect(dialog.locator("[data-field=where]")).to_contain_text("Automatic: QuickBook spare")
        expect(dialog.get_by_text("Please Select")).to_have_count(0)
        dialog.locator("[data-action=quickbook-submit]").click()
        expect(dialog).to_be_hidden()
        assert page.url.endswith("/devenv")  # no jump to the schedule

        mine = api(page, server, "GET", "/api/reservations?mine=true")
        created = [r for r in mine if r["lane_id"] == lane["id"] and r["phase"] != "finished"]
        assert len(created) == 1
        booking = created[0]
        assert booking["lane_id"] == lane["id"]
        assert booking["devenv_template_id"] is not None
        length = datetime.fromisoformat(booking["end_at"]) - datetime.fromisoformat(booking["start_at"])
        assert timedelta(hours=1, minutes=59) <= length <= timedelta(hours=2, minutes=1)

        # now every lane is taken: QuickBook says by whom; "More options" opens the full dialog
        page.goto(f"{server}/schedule")
        page.get_by_role("button", name="New booking").first.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_text("Whole GPU", exact=True).click()
        expect(dialog.locator("[data-field=refusal]")).to_contain_text("booked by admin")
        expect(dialog.locator("[data-action=quickbook-submit]")).to_be_disabled()
        dialog.locator("[data-action=more-options]").click()
        expect(page.locator("[data-action=more-options]")).to_have_count(0)
        expect(page.get_by_role("dialog").get_by_text("Book the whole GPU", exact=True)).to_be_visible()
        page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
    finally:
        for r in [*held, *created]:
            page.request.fetch(f"{server}/api/reservations/{r['id']}", method="DELETE", headers={"X-SlotServe": "1"})
        page.request.fetch(f"{server}/api/admin/lanes/{lane['id']}", method="DELETE", headers={"X-SlotServe": "1"})
        api(page, server, "DELETE", "/api/devenv/password")
        api(page, server, "PUT", "/api/admin/settings", {"devenv_enabled": None})
    assert not page.errors  # type: ignore[attr-defined]

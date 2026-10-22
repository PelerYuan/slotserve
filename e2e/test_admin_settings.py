"""Admin UI of W1-07: grouped admin sections, the settings page (cards, search, preview with notification, history and
undo), the audit log filters, per-person extra hours and the health / setup checklist on the admin overview."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from playwright.sync_api import Browser, Page, expect

from conftest import ROOT
from helpers import INVITE, PASSWORD, api, sign_in

TABS = ["overview", "runtime", "lanes", "storage", "users", "reservations", "keys", "channels", "settings", "audit"]
TABS += ["requests"]


def _member(browser: Browser, server: str, name: str):
    """A member account of its own (signed up with the invite code), as a browser context."""
    ctx = browser.new_context()
    r = ctx.request.post(
        f"{server}/api/auth/register",
        data={"username": name, "password": PASSWORD, "invite_code": INVITE},
        headers={"X-SlotServe": "1"},
    )
    assert r.ok, r.text()
    return ctx


def _call(ctx, server: str, method: str, path: str, data=None):
    r = ctx.request.fetch(f"{server}{path}", method=method, data=data, headers={"X-SlotServe": "1"})
    assert r.ok, f"{method} {path}: {r.status} {r.text()}"
    return r.json() if r.status != 204 else None


def _setting(page: Page, server: str, key: str):
    return {s["key"]: s for s in api(page, server, "GET", "/api/admin/settings")}[key]["value"]


def test_admin_sections_fit_and_are_grouped(page: Page, server: str):
    page.set_viewport_size({"width": 1440, "height": 900})
    sign_in(page, server, "admin")
    page.goto(f"{server}/admin")
    tabs = page.get_by_role("tab")
    expect(tabs).to_have_count(len(TABS))
    for name in TABS:
        box = page.locator(f"[data-tab={name}]").bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 1440, (name, box)
    expect(page.get_by_role("tab", name="Request log")).to_be_visible()
    page.get_by_role("tab", name="Audit log").click()
    expect(page).to_have_url(f"{server}/admin?tab=audit")
    expect(page.locator("[data-tab=audit]")).to_have_attribute("aria-selected", "true")

    # a narrow screen gets a list that shows the current section
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("[data-tab=audit]")).to_be_hidden()
    expect(page.locator(".nav-select")).to_contain_text("Audit log")
    assert not page.errors  # type: ignore[attr-defined]


def test_settings_cards_search_and_changed_only(page: Page, server: str):
    page.set_viewport_size({"width": 1440, "height": 900})
    sign_in(page, server, "admin")
    page.goto(f"{server}/admin?tab=settings")
    # the basic view: policy cards with the main policies in one screen
    for key in ("registration_open", "invite_code", "approval_mode", "quota_weekly_hours", "max_reservation_hours"):
        row = page.locator(f"[data-card] [data-setting={key}]")
        expect(row).to_be_visible()
        box = row.bounding_box()
        assert box and box["y"] + box["height"] <= 900, (key, box)
    expect(page.locator("[data-setting=devenv_enabled]")).to_be_visible()
    expect(page.locator("[data-setting=timezone]")).to_be_visible()
    expect(page.locator("[data-testid=chat-platforms]")).to_be_visible()
    expect(page.locator("[data-setting=scheduler_interval_seconds]")).to_have_count(0)  # expert: not in the cards

    search = page.locator("[data-testid=settings-search] input")
    for q in ("quota", "QUOTA_WEEKLY", "配额"):
        search.fill(q)
        expect(page.locator("[data-group] [data-setting=quota_weekly_hours]")).to_be_visible()
    search.fill("端口")
    expect(page.locator("[data-group] [data-setting=devenv_ssh_ports]")).to_be_visible()
    expect(page.locator("[data-group] [data-setting=devenv_public_ports]")).to_be_visible()
    expect(page.locator("[data-group=deployment] [data-setting=devenv_web_port]")).to_be_visible()  # read-only
    search.fill("")

    api(page, server, "PUT", "/api/admin/settings", {"slot_minutes": 15})
    try:
        page.reload()
        page.locator("[data-testid=changed-only]").click()
        expect(page.locator("[data-group] [data-setting=slot_minutes]")).to_be_visible()
        expect(page.locator("[data-group] [data-setting=registration_open]")).to_have_count(0)
    finally:
        api(page, server, "PUT", "/api/admin/settings", {"slot_minutes": None})
    assert not page.errors  # type: ignore[attr-defined]


def test_quota_preview_notifies_and_undo_restores(page: Page, server: str, browser: Browser):
    member = _member(browser, server, "w107-quota")
    try:
        lane = _call(member, server, "GET", "/api/lanes")[0]["id"]
        model = _call(member, server, "GET", "/api/models")[0]["id"]
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1, hours=2)
        booking = _call(
            member,
            server,
            "POST",
            "/api/reservations",
            {
                "lane_id": lane,
                "model_id": model,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=5)).isoformat(),
            },
        )[0]
        sign_in(page, server, "admin")
        page.goto(f"{server}/admin?tab=settings")
        field = page.locator("[data-card] [data-setting=quota_weekly_hours] input")
        field.fill("2")
        field.press("Tab")
        page.locator("[data-save=fairness]").click()
        preview = page.locator("[data-testid=settings-preview]")
        expect(preview).to_contain_text("Weekly hours per person")
        expect(preview.locator("[data-impact=quota_weekly_hours]")).to_contain_text("w107-quota")
        preview.locator("[data-testid=preview-notify]").click()
        preview.locator("[data-testid=preview-confirm]").click()
        expect(preview).to_be_hidden()
        assert _setting(page, server, "quota_weekly_hours") == 2

        # the booking is kept and its owner was told
        mine = _call(member, server, "GET", "/api/me/notifications")
        assert any(n["event"] == "policy_quota_weekly_hours" for n in mine["items"]), mine
        end = start + timedelta(hours=6)
        window = urlencode({"start": start.isoformat(), "end": end.isoformat(), "mine": "true"})
        kept = _call(member, server, "GET", f"/api/reservations?{window}")
        assert any(r["id"] == booking["id"] for r in kept)

        # history of the setting, then undo through the preview
        page.locator("[data-view=all]").click()
        row = page.locator("[data-group] [data-setting=quota_weekly_hours]")
        row.get_by_text("History").click()
        history = page.locator("[data-testid=setting-history]")
        expect(history).to_contain_text("Unlimited")
        history.get_by_role("button", name="Undo").first.click()
        page.locator("[data-testid=settings-preview] [data-testid=preview-confirm]").click()
        expect(page.locator("[data-testid=settings-preview]")).to_be_hidden()
        assert _setting(page, server, "quota_weekly_hours") == 0
    finally:
        api(page, server, "PUT", "/api/admin/settings", {"quota_weekly_hours": None})
        member.close()
    assert not page.errors  # type: ignore[attr-defined]


def test_audit_filters_and_csv(page: Page, server: str):
    sign_in(page, server, "admin")
    api(page, server, "PUT", "/api/admin/settings", {"reminder_minutes": 20})
    api(page, server, "PUT", "/api/admin/settings", {"reminder_minutes": None})
    page.goto(f"{server}/admin?tab=audit")
    page.locator("[data-testid=audit-filters] .f-action").click()
    page.keyboard.type("All: Settings")  # the list is virtual: filter it to the option
    page.locator(".n-base-select-option", has_text="All: Settings").click()
    page.keyboard.press("Escape")
    rows = page.locator("tr[data-audit]")
    expect(rows.first).to_have_attribute("data-audit", "settings.update")
    expect(rows.first).to_contain_text("Reminder lead time")
    expect(rows.first).to_contain_text("→")
    assert "action=settings.*" in page.locator("[data-testid=audit-csv]").get_attribute("href").replace("%2A", "*")
    csv = page.request.get(f"{server}/api/admin/audit.csv?action=settings.*")
    assert csv.ok and "settings.update" in csv.text()
    assert not page.errors  # type: ignore[attr-defined]


def test_extra_hours_for_one_person(page: Page, server: str, browser: Browser):
    member = _member(browser, server, "w107-thesis")
    member.close()
    sign_in(page, server, "admin")
    api(page, server, "PUT", "/api/admin/settings", {"quota_weekly_hours": 12})  # extra hours count with a quota
    try:
        _give_extra_hours(page, server)
    finally:
        api(page, server, "PUT", "/api/admin/settings", {"quota_weekly_hours": None})
    assert not page.errors  # type: ignore[attr-defined]


def _give_extra_hours(page: Page, server: str):
    page.goto(f"{server}/admin?tab=users")
    expect(page.locator("tr", has_text="w107-thesis")).to_contain_text("0 of 12 h")
    row = page.locator("tr", has_text="w107-thesis")
    row.locator("button").last.click()
    page.get_by_text("Limits and extra hours").click()
    dialog = page.locator("[data-testid=user-limits]")
    dialog.locator("[data-testid=give-hours]").click()
    dialog.locator("[data-field=grant-hours] input").fill("10")
    dialog.locator("[data-field=grant-reason] input").fill("thesis defence")
    dialog.locator("[data-testid=grant-save]").click()
    expect(dialog).to_contain_text("thesis defence")
    users = {u["username"]: u["id"] for u in api(page, server, "GET", "/api/admin/users")}
    policy = api(page, server, "GET", f"/api/admin/users/{users['w107-thesis']}/policy")
    assert policy["extra_hours"] == 10 and policy["grants"][0]["reason"] == "thesis defence"
    expect(page.locator("tr", has_text="w107-thesis")).to_contain_text("+10 h")
    dialog.get_by_role("button", name="Withdraw").click()
    expect(dialog).to_contain_text("withdrawn")


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_overview_checklist_and_health(page: Page, server: str, locale: str):
    sign_in(page, server, "admin")
    if locale != "en-US":
        page.evaluate("localStorage.setItem('slotserve.locale', 'zh-CN')")
    page.goto(f"{server}/admin")
    checklist = page.locator("[data-testid=setup-checklist]")
    health = page.locator("[data-testid=health-table]")
    expect(health).to_be_visible()
    checks = api(page, server, "GET", "/api/health")["checks"]
    # the table shows every check that is not dismissed, the checklist the onboarding ones
    shown = [c for c in checks if not c["dismissed"]]
    expect(health.locator("tr.check-row")).to_have_count(len(shown))
    if checklist.locator(".items").count() == 0:
        checklist.get_by_role("button").last.click()  # collapsed: everything required is done
    expect(checklist.locator("li[data-check]")).to_have_count(sum(1 for c in checks if c["onboarding"]))
    if locale != "en-US":
        page.evaluate("localStorage.setItem('slotserve.locale', 'en')")
        return
    # dismiss the chat check with a reason, then show it again
    chat = next(c for c in checks if c["id"] == "chat")
    if chat["dismissible"] and not chat["dismissed"] and chat["level"] != "ok":
        row = health.locator("tr.check-row[data-check=chat]")
        row.locator("td.right [data-action=dismiss]").click()
        dialog = page.get_by_role("dialog")
        dialog.locator("textarea").fill("we use e-mail")
        dialog.get_by_role("button", name="Dismiss").click()
        expect(health.locator("tr.check-row[data-check=chat]")).to_have_count(0)
        state = next(c for c in api(page, server, "GET", "/api/health")["checks"] if c["id"] == "chat")
        assert state["dismissed"]["reason"] == "we use e-mail"
        api(page, server, "DELETE", "/api/health/checks/chat/dismiss")
    assert not page.errors  # type: ignore[attr-defined]


def _label_keys(source: str) -> set[str]:
    """Property names of the `admin.settings` texts in one i18n source (labels, `<key>_desc`, ...)."""
    start = source.index("    settings: {", source.index("  admin: {"))
    end = source.index("\n    },", start)
    return set(re.findall(r"^      ([a-z_0-9]+):", source[start:end], re.M))


def test_every_registry_setting_has_a_label_in_both_languages():
    """The settings page is built from the registry: each setting needs a label (and a description unless it is a
    read-only deployment parameter) in English and Chinese, in i18n/*.ts or features/settings/i18n.ts."""
    from slotserve.services import app_settings

    src = ROOT / "frontend" / "src"
    feature_en, feature_zh = (
        (src / "features" / "settings" / "i18n.ts").read_text(encoding="utf-8").split("export const zh")
    )
    for lang, base, feature in (("en", "en.ts", feature_en), ("zh-CN", "zh-CN.ts", feature_zh)):
        keys = _label_keys((src / "i18n" / base).read_text(encoding="utf-8")) | _label_keys(feature)
        missing = [k for k in app_settings.REGISTRY if k not in keys]
        assert not missing, f"{lang}: settings without a label: {missing}"
        missing = [
            k for k, spec in app_settings.REGISTRY.items() if not spec.restart_required and f"{k}_desc" not in keys
        ]
        assert not missing, f"{lang}: settings without a description: {missing}"

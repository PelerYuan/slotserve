"""Wording checks (F14): every page and main dialog, in English and Chinese.

No input or select shows the component library's default placeholder ("Please Input", "请选择", ...), no name is shown
twice ("GPU 0 · GPU 0"), an empty GPU list is never shown as "gpus=", and audit actions are translated. Runs its own
seeded server, so the users it registers do not depend on the order of the other e2e tests.
"""

import os
import re
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import Browser, Page, expect

from conftest import ROOT, _free_port
from helpers import INVITE, PASSWORD

# Naive UI's own placeholders (enUS / zhCN locales): seeing one means an input has no placeholder of ours.
DEFAULT_PLACEHOLDERS = {
    "Please Input",
    "Please Select",
    "Select Date",
    "Select Date and Time",
    "Select Time",
    "请输入",
    "请选择",
    "选择日期",
    "选择日期时间",
    "请选择时间",
}
DUPLICATE_NAME = re.compile(r"\b((?:GPU|lane|通道) ?[\w,.-]+) · \1(?![\w,])")
EMPTY_GPUS = re.compile(r"\bgpus=(?=\s|$)")
RAW_ACTION = re.compile(r"^[a-z_]+\.[a-z_]+$")
# A translation key shown as is, e.g. "models.pathHint.llama.cpp" (a dot inside a key part breaks the lookup).
RAW_KEY = re.compile(
    r"\b(?:app|nav|common|auth|state|overview|schedule|models|catalog|keys|devenv|usage|settings|admin|errors|ph|naming)"
    r"\.[a-z][A-Za-z_]+(?:\.[\w.]+)?"
)

PAGES = ["/", "/schedule", "/models", "/keys", "/devenv", "/usage", "/settings"]
ADMIN_TABS = ["overview", "users", "lanes", "reservations", "requests", "keys", "channels", "storage", "runtime"]
ADMIN_TABS += ["settings", "audit"]

# Buttons that open the main dialogs, per locale: (page, button text).
DIALOGS = {
    "en-US": [
        ("/schedule", "New booking"),
        ("/keys", "New key"),
        ("/models", "Add model"),
        ("/devenv", "New template"),
        ("/admin?tab=lanes", "Add lane"),
        ("/admin?tab=users", "New user"),
        ("/admin?tab=storage", "Add storage"),
    ],
    "zh-CN": [
        ("/schedule", "新建预约"),
        ("/keys", "新建密钥"),
        ("/models", "添加模型"),
        ("/devenv", "新建模板"),
        ("/admin?tab=lanes", "新建通道"),
        ("/admin?tab=users", "新建用户"),
        ("/admin?tab=storage", "添加存储"),
    ],
}
BOOKING_TITLES = {
    "en-US": {
        "model": "Book GPU time for a model",
        "exclusive": "Book the whole GPU",
        "devenv": "Book a dev environment",
    },
    "zh-CN": {"model": "预约 GPU 运行模型", "exclusive": "预约整块 GPU", "devenv": "预约开发环境"},
}
KIND_BUTTONS = {
    "en-US": {"exclusive": "Whole GPU", "devenv": "Dev environment"},
    "zh-CN": {"exclusive": "整块 GPU", "devenv": "开发环境"},
}

SNAPSHOT_JS = """() => {
  const shown = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
  const placeholders = [
    ...[...document.querySelectorAll('input, textarea')].filter(shown).map((e) => e.getAttribute('placeholder')),
    ...[...document.querySelectorAll('.n-base-selection-placeholder, .n-input__placeholder')]
      .filter(shown)
      .map((e) => e.innerText),
  ]
  const actions = [...document.querySelectorAll('table .badge')].map((e) => e.innerText.trim())
  return { text: document.body.innerText, placeholders: placeholders.filter((p) => p), actions }
}"""


@pytest.fixture(scope="module")
def server(tmp_path_factory, browser: Browser):
    """A server of its own: an admin, a member, two lanes, a storage and dev environments switched on."""
    tmp = tmp_path_factory.mktemp("slotserve-copy")
    models = tmp / "models"
    models.mkdir()
    for name in ("chat-small.gguf", "chat-large.gguf"):
        (models / name).write_bytes(b"GGUF" + b"\0" * 2048)
    port = _free_port()
    env = {
        **os.environ,
        "SLOTSERVE_PORT": str(port),
        "SLOTSERVE_HOST": "127.0.0.1",
        "SLOTSERVE_DATA_DIR": str(tmp / "data"),
        "SLOTSERVE_MODELS_DIR": str(models),
        "SLOTSERVE_INVITE_CODE": INVITE,
        "SLOTSERVE_SECRET_KEY": "e2e-secret",
        "SLOTSERVE_LLAMA_SERVER_CMD": f'"{sys.executable}" "{ROOT / "tools" / "mock_llama_server.py"}"',
        "SLOTSERVE_LLAMA_PORT": str(_free_port()),
        "SLOTSERVE_DEVENV_WEB_PORT": str(_free_port()),
        "SLOTSERVE_SCHEDULER_INTERVAL_SECONDS": "0.5",
        "MOCK_LOAD_SECONDS": "1",
        "MOCK_TOKEN_DELAY": "0",
    }
    log = (tmp / "server.log").open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "slotserve"], cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=1)
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError((tmp / "server.log").read_text(errors="replace")[-4000:]) from None
                time.sleep(0.3)
        _seed(browser, base)
        yield base
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _call(ctx, base: str, method: str, path: str, data=None):
    resp = ctx.request.fetch(f"{base}{path}", method=method, data=data)
    assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
    return resp.json() if resp.status != 204 else None


def _seed(browser: Browser, base: str) -> None:
    ctx = browser.new_context()
    try:
        _call(
            ctx, base, "POST", "/api/auth/register", {"username": "admin", "password": PASSWORD, "invite_code": INVITE}
        )
        _call(ctx, base, "POST", "/api/models/scan")
        models = {m["name"]: m["id"] for m in _call(ctx, base, "GET", "/api/models")}
        _call(ctx, base, "POST", f"/api/models/{models['chat-small']}/default")
        # Name the first lane after its GPU, the way a single-GPU lane usually is: it must still be shown once.
        gpus = [g["index"] for g in _call(ctx, base, "GET", "/api/status").get("gpus") or []]
        first = _call(ctx, base, "GET", "/api/lanes")[0]
        body = {
            "name": f"GPU {gpus[0]}" if gpus else "GPU 0",
            "gpus": gpus[:1],
            "default_model_id": models["chat-small"],
        }
        _call(ctx, base, "PATCH", f"/api/admin/lanes/{first['id']}", body)
        # A second lane without a GPU list ("all visible GPUs"), which used to show up as "gpus=" in the audit log.
        _call(
            ctx,
            base,
            "POST",
            "/api/admin/lanes",
            {"name": "GPU 1", "gpus": [], "default_model_id": models["chat-large"]},
        )
        _call(ctx, base, "PUT", "/api/admin/settings", {"devenv_enabled": True})
        storage = {
            "name": "Shared datasets",
            "kind": "host",
            "purpose": "shared",
            "host_path": "/srv/datasets",
            "mount_path": "/data",
            "read_only": True,
        }
        _call(ctx, base, "POST", "/api/admin/storage", storage)
    finally:
        ctx.close()
    member = browser.new_context()
    try:
        _call(
            member,
            base,
            "POST",
            "/api/auth/register",
            {"username": "member", "password": PASSWORD, "invite_code": INVITE},
        )
    finally:
        member.close()


def _open(browser: Browser, base: str, locale: str, user: str = "admin") -> Page:
    ctx = browser.new_context(viewport={"width": 1366, "height": 860}, locale=locale)
    _call(ctx, base, "POST", "/api/auth/login", {"username": user, "password": PASSWORD})
    page = ctx.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("pageerror", lambda e: page.errors.append(str(e)))  # type: ignore[attr-defined]
    return page


def _problems(page: Page, where: str) -> list[str]:
    snap = page.evaluate(SNAPSHOT_JS)
    out = [f"{where}: default placeholder {p!r}" for p in snap["placeholders"] if p.strip() in DEFAULT_PLACEHOLDERS]
    out += [f"{where}: default placeholder text {w!r}" for w in ("Please Input", "Please Select") if w in snap["text"]]
    out += [f"{where}: name shown twice {m.group(0)!r}" for m in DUPLICATE_NAME.finditer(snap["text"])]
    out += [f"{where}: empty GPU list shown as 'gpus='" for _ in EMPTY_GPUS.finditer(snap["text"])]
    out += [f"{where}: untranslated audit action {a!r}" for a in snap["actions"] if RAW_ACTION.match(a)]
    out += [f"{where}: translation key shown {m.group(0)!r}" for m in RAW_KEY.finditer(snap["text"])]
    return out


def _settle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_pages_have_no_default_placeholders_or_repeated_names(browser: Browser, server: str, locale: str):
    page = _open(browser, server, locale)
    problems: list[str] = []
    for path in PAGES + [f"/admin?tab={tab}" for tab in ADMIN_TABS]:
        page.goto(f"{server}{path}")
        _settle(page)
        problems += _problems(page, f"{locale} {path}")
    assert not problems, "\n".join(problems)
    assert not page.errors  # type: ignore[attr-defined]
    page.context.close()


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_dialogs_have_no_default_placeholders(browser: Browser, server: str, locale: str):
    page = _open(browser, server, locale)
    problems: list[str] = []
    for path, button in DIALOGS[locale]:
        page.goto(f"{server}{path}")
        _settle(page)
        page.get_by_role("button", name=button).first.click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        _settle(page)
        problems += _problems(page, f"{locale} {path} [{button}]")
    assert not problems, "\n".join(problems)
    assert not page.errors  # type: ignore[attr-defined]
    page.context.close()


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_booking_dialog_title_follows_the_booking_type(browser: Browser, server: str, locale: str):
    page = _open(browser, server, locale, user="member")
    page.goto(f"{server}/schedule")
    _settle(page)
    page.get_by_role("button", name=DIALOGS[locale][0][1]).first.click()
    dialog = page.get_by_role("dialog")
    titles = BOOKING_TITLES[locale]
    expect(dialog.get_by_text(titles["model"], exact=True)).to_be_visible()
    for kind in ("exclusive", "devenv"):
        dialog.locator(".n-radio-button", has_text=KIND_BUTTONS[locale][kind]).first.click()
        expect(dialog.get_by_text(titles[kind], exact=True)).to_be_visible()
        problems = _problems(page, f"{locale} booking dialog ({kind})")
        assert not problems, "\n".join(problems)
    # the lane list names each lane once ("GPU 0", not "GPU 0 · GPU 0")
    dialog.locator(".n-base-selection").first.click()
    expect(page.locator(".n-base-select-option").first).to_be_visible()
    options = page.locator(".n-base-select-option").all_inner_texts()
    assert options and not any(DUPLICATE_NAME.search(o) for o in options), options
    page.context.close()


@pytest.mark.xfail(reason="LoginView.vue is owned by W0-01; its inputs still need placeholders", strict=False)
@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_login_page_has_no_default_placeholders(browser: Browser, server: str, locale: str):
    ctx = browser.new_context(locale=locale)
    page = ctx.new_page()
    page.goto(f"{server}/login")
    _settle(page)
    problems = _problems(page, f"{locale} /login")
    ctx.close()
    assert not problems, "\n".join(problems)


def test_every_input_in_the_source_sets_a_placeholder():
    """Selects with a value hide their placeholder in the browser, so check the templates themselves too."""
    tag = re.compile(
        r"<(NInput|NSelect|NInputNumber|NDatePicker|NTimePicker|NAutoComplete)\b((?:\"[^\"]*\"|'[^']*'|[^'\">])*)>",
        re.S,
    )
    missing = []
    for path in sorted((ROOT / "frontend" / "src").rglob("*.vue")):
        if path.name == "LoginView.vue":  # owned by W0-01, see the xfail test above
            continue
        text = path.read_text(encoding="utf-8")
        for m in tag.finditer(text):
            if "placeholder" not in m.group(2):
                line = text.count("\n", 0, m.start()) + 1
                missing.append(f"{path.relative_to(ROOT).as_posix()}:{line} <{m.group(1)}>")
    assert not missing, "inputs without a placeholder:\n" + "\n".join(missing)


def test_audit_action_names_are_translated():
    """Every audit action the server records has a label in both languages."""
    backend = ROOT / "backend" / "slotserve"
    recorded = set()
    for path in backend.rglob("*.py"):
        for call in re.findall(r"\brecord\((.*?)\)\n", path.read_text(encoding="utf-8"), re.S):
            recorded |= set(re.findall(r"\"([a-z_]+\.[a-z_]+)\"", call))
    # actions built from a variable: f"reservation.{action}" and f"runtime.{body.action}"
    recorded |= {"reservation.cancel", "reservation.release", "runtime.load", "runtime.unload", "runtime.auto"}
    assert len(recorded) > 20
    for lang in ("en", "zh-CN"):
        source = (ROOT / "frontend" / "src" / "i18n" / f"{lang}.ts").read_text(encoding="utf-8")
        block = source[source.index("    audit: {") :]
        block = block[block.index("actions: {") : block.index("},")]
        keys = set(re.findall(r"^\s*([a-z_]+):", block, re.M))
        missing = sorted(a for a in recorded if a.replace(".", "_") not in keys)
        assert not missing, f"{lang}: audit actions without a label: {missing}"


def test_copy_check_finds_what_it_looks_for():
    """The patterns above catch the problems they are meant to catch."""
    assert DUPLICATE_NAME.search("GPU 0 · GPU 0")
    assert DUPLICATE_NAME.search("Lanes GPU 0,1 · GPU 0,1 x")
    assert not DUPLICATE_NAME.search("GPU 0 · GPU 1")
    assert not DUPLICATE_NAME.search("GPU 0 · GPU 0,1")
    assert EMPTY_GPUS.search("lane GPU 1  gpus=")
    assert not EMPTY_GPUS.search("gpus=0 idle_mode=model")
    assert RAW_ACTION.match("storage.create") and not RAW_ACTION.match("Added storage")
    assert RAW_KEY.search("Model models.pathHint.llama.cpp")
    assert not RAW_KEY.search("/models/Qwen3-8B-Q4_K_M.gguf, see Admin → Settings.")

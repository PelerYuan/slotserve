"""CSRF (R1-02) in a real browser: SlotServe's own pages send X-SlotServe; a page on another port of the same
host (like a dev environment's direct port) cannot make a signed-in browser change anything."""

import http.server
import socket
import threading

import pytest
from playwright.sync_api import Page

from helpers import api, sign_in


def test_frontend_sends_the_csrf_header(page: Page, server: str):
    writes = []
    page.on(
        "request",
        lambda r: writes.append(r) if "/api/" in r.url and r.method not in ("GET", "HEAD") else None,
    )
    sign_in(page, server, "admin")  # POST /api/auth/login through the API client
    page.goto(f"{server}/settings")
    page.get_by_text("Dark", exact=True).click()
    page.wait_for_load_state("networkidle")
    assert writes, "no changing request was made"
    for r in writes:
        assert r.headers.get("x-slotserve") == "1", f"{r.method} {r.url} without X-SlotServe"


@pytest.fixture
def other_port_site():
    """A page served on another port of the same host (same site as SlotServe, different origin)."""
    pages: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, "").encode()
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", pages
    httpd.shutdown()
    httpd.server_close()


def test_page_on_another_port_cannot_post(page: Page, server: str, other_port_site):
    base, pages = other_port_site
    sign_in(page, server, "admin")
    before = len(api(page, server, "GET", "/api/admin/backups"))
    target = f"{server}/api/admin/backups"
    pages["/fetch"] = f"""<script>
      window.done = Promise.all([
        fetch({target!r}, {{method: 'POST', mode: 'no-cors', credentials: 'include'}}),
        fetch({target!r}, {{method: 'POST', mode: 'no-cors', credentials: 'include', referrerPolicy: 'no-referrer',
                            headers: {{'content-type': 'text/plain'}}, body: 'x'}}),
      ]).then(() => 'sent', e => 'error ' + e)
    </script>"""
    pages["/form"] = f"""<iframe name="sink"></iframe>
      <form id="f" method="post" action={target!r} target="sink"><input name="a" value="b"></form>
      <script>document.getElementById('f').submit()</script>"""

    page.goto(f"{base}/fetch")
    assert page.evaluate("window.done") == "sent"
    page.goto(f"{base}/form")
    page.wait_for_timeout(1500)
    page.goto(f"{server}/")
    assert len(api(page, server, "GET", "/api/admin/backups")) == before
    # the same request from SlotServe's own page works (the session is valid)
    api(page, server, "POST", "/api/admin/backups")
    assert len(api(page, server, "GET", "/api/admin/backups")) == before + 1

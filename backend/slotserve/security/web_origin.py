"""Who may act inside a dev environment's web interface on the shared web origin (R1-03).

All environments' web interfaces share one origin (``devenv_web_port``, e.g. ``:8001``) and are told apart only
by their path ``/devenv/<id>/``. A path is not a security boundary for browsers: a page of environment A can send
requests to ``/devenv/B/...`` and the browser adds B's cookie. The proxy also adds B's app token on the way to
the container (Jupyter, the desktop), so such a request would run with B's rights. This module decides, from
what the browser says about a request (Fetch Metadata ``Sec-Fetch-*``, ``Referer``, ``Origin``), whether it may
reach the environment at all, and whether it is *trusted*: only trusted requests get the app token and the app's
own cookies. Everything not positively recognized is refused or untrusted (fail-closed):

* ``Sec-Fetch-Site: same-origin`` (a page on the web origin made it): the ``Referer`` must be a page of the same
  environment; a missing ``Referer`` (``no-referrer``) is refused, not waved through. Responses carry
  ``Referrer-Policy: same-origin`` so the apps' own requests have one.
* ``none`` (address bar, bookmark, reload): trusted for GET/HEAD.
* ``same-site`` / ``cross-site`` (SlotServe's own pages, direct ports, other sites): only top-level GET/HEAD
  navigations, never sub-requests, frames or form posts.
* WebSocket handshakes carry neither ``Referer`` nor ``Sec-Fetch-*``; their ``Origin`` must be the web origin.
* Requests without Fetch Metadata (scripts, very old browsers) pass, but untrusted: no token, no app cookies.

A ``Referer`` can be made to name another environment by a page that rewrites its own URL
(``history.replaceState``), and WebSockets carry no ``Referer`` at all, so the Referer rule alone is not enough
(no Referer-only trust). The web listener therefore also lets one browser hold only one *trust group* at a time
(``scope``, see ``trust_group`` and ``gateway/devenv_web_auth.py``): the environments of one person, or one shared
environment. Opening another group's environment switches the browser over and the previous one stops working
there until it is opened again through SlotServe. What remains is a page of an environment that stays open in a
tab while its person switches to another group; only one origin per environment closes that (M2, D01: port pool).

``devenv_web_compat`` (an admin setting) turns off the two cross-environment rules (Referer and scope) for apps
that break under them; it never relaxes the other rules nor SlotServe's own CSRF protection.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .csrf import _host_matches, _origin_parts

SAFE_METHODS = frozenset({"GET", "HEAD"})
ENV_PATH = re.compile(r"^/devenv/(\d+)/")
# headers the proxy decides itself on the web origin (an app's own values are dropped)
REFERRER_POLICY = "same-origin"
OPENER_POLICY = "same-origin"
FRAME_POLICY = "frame-ancestors 'self'"
DENIED_POLICY = "sandbox; frame-ancestors 'none'"  # a refusal page can be neither scripted nor framed


@dataclass(frozen=True)
class Verdict:
    allow: bool
    trusted: bool  # the app token and the app's own cookies go to the container
    reason: str  # why (logged for refusals)
    navigation: bool = False  # a top-level page load (refusals are shown as a page)


def environment_of(url: str) -> int | None:
    """The environment a URL on the web origin belongs to (``/devenv/<id>/...``), if any."""
    try:
        m = ENV_PATH.match(urlsplit(url).path)
    except ValueError:
        return None
    return int(m.group(1)) if m else None


def same_origin(origin: str, headers: dict[str, str], web_url: str = "") -> bool:
    """Whether an ``Origin`` header names the origin the request was sent to (the web origin)."""
    parts = _origin_parts(origin) if origin and origin.strip().lower() != "null" else None
    if parts is None:
        return False
    if web_url and _origin_parts(web_url) == parts:
        return True
    hosts = [headers.get("host", "")]
    if forwarded := headers.get("x-forwarded-host"):
        hosts.append(forwarded.split(",")[0])
    return any(_host_matches(h, parts) for h in hosts)


def classify(
    method: str,
    headers: dict[str, str],
    session_id: int,
    *,
    websocket: bool = False,
    compat: bool = False,
    own_pages: bool = False,
    web_url: str = "",
) -> Verdict:
    """The rules in the module docstring. ``headers`` have lower-case names. ``own_pages``: SlotServe's own pages
    share the origin (``devenv_web_port`` 0), so they may open an environment by a link."""
    method = method.upper()
    if websocket:
        origin = headers.get("origin")
        if not origin:
            return Verdict(True, False, "no_origin")  # not a browser
        if not same_origin(origin, headers, web_url):
            return Verdict(False, False, "websocket_foreign_origin")
        referer = headers.get("referer")  # browsers send none today; if one does, it must fit
        if referer and not compat and environment_of(referer) not in (None, session_id):
            return Verdict(False, False, "other_environment")
        return Verdict(True, True, "websocket_same_origin")

    site = headers.get("sec-fetch-site", "").strip().lower()
    mode = headers.get("sec-fetch-mode", "").strip().lower()
    dest = headers.get("sec-fetch-dest", "").strip().lower()
    top_level = mode == "navigate" and dest in ("document", "")
    safe = method in SAFE_METHODS
    if not site:
        return Verdict(True, False, "no_fetch_metadata")
    if site == "none":
        if safe:
            return Verdict(True, True, "user_navigation", navigation=top_level)
        return Verdict(False, False, "unsafe_without_initiator", navigation=top_level)
    if site in ("same-site", "cross-site"):
        if top_level and safe:
            return Verdict(True, True, "navigation", navigation=True)
        return Verdict(False, False, "cross_site_" + (mode or "request"), navigation=top_level)
    if site != "same-origin":
        return Verdict(False, False, "unknown_fetch_site", navigation=top_level)
    if compat:
        return Verdict(True, True, "compat", navigation=top_level)
    referer = headers.get("referer", "")
    if not referer:
        return Verdict(False, False, "referer_missing", navigation=top_level)
    env = environment_of(referer)
    if env == session_id:
        return Verdict(True, True, "same_environment", navigation=top_level)
    if env is None and own_pages and top_level and safe:
        return Verdict(True, True, "own_page_link", navigation=True)
    return Verdict(False, False, "other_environment" if env is not None else "referer_foreign", navigation=top_level)


def trust_group(session) -> str:
    """Environments a browser may hold together on the shared origin: all of one person's, or a single shared one
    (anyone can put pages into a lane's shared environment)."""
    kind = getattr(session, "kind", None) or ("shared" if session.reservation_id is None else "booking")
    if kind == "shared" or session.user_id is None:
        return f"env:{session.id}"
    return f"user:{session.user_id}"


def response_headers(items: list[tuple[str, str]], *, denied: bool = False) -> list[tuple[str, str]]:
    """Response headers on the web origin: the app's Referrer-Policy and Cross-Origin-Opener-Policy are replaced
    (pages always send a Referer to their own origin, and no page keeps a handle on a window that leaves it);
    framing is limited to the origin itself; refusals are sandboxed."""
    drop = {"referrer-policy", "cross-origin-opener-policy"}
    if denied:
        drop |= {"content-security-policy", "x-frame-options"}
    out = [(k, v) for k, v in items if k.lower() not in drop]
    out += [("referrer-policy", REFERRER_POLICY), ("cross-origin-opener-policy", OPENER_POLICY)]
    if denied:
        out += [("content-security-policy", DENIED_POLICY), ("x-frame-options", "DENY"), ("cache-control", "no-store")]
    else:
        out.append(("content-security-policy", FRAME_POLICY))
    return out

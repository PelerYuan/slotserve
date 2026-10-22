"""security/credentials.py: prefixes, scopes, API keys, environment keys and invites (in-memory database)."""

from datetime import timedelta

import pytest

from slotserve import orm
from slotserve.api.playground import RateLimiter
from slotserve.db import utcnow
from slotserve.security import credentials as cr
from slotserve.security import hash_api_key
from slotserve.security.credentials import CredentialError


def test_prefixes_tell_the_credentials_apart():
    assert cr.new_secret(cr.API_KEY_PREFIX).startswith("sk-ss-")
    assert cr.new_secret(cr.ENV_KEY_PREFIX).startswith("sk-ss-env-")
    assert cr.new_secret(cr.INVITE_PREFIX).startswith("ssi-")
    raw = cr.new_secret(cr.ENV_KEY_PREFIX)
    assert cr.display_prefix(raw, cr.ENV_KEY_PREFIX) == raw[:14]
    assert cr.digest(raw) == hash_api_key(raw)  # keys made before 0.10 keep matching


@pytest.mark.parametrize("scope", ["admin", "llm admin", "", "api:env"])
def test_no_admin_or_unknown_scope(scope):
    with pytest.raises(ValueError):
        cr.normalize_scope(scope)


def test_scope_normalized():
    assert cr.normalize_scope(" llm  llm ") == "llm"
    assert cr.has_scope("llm", "llm") and not cr.has_scope("llm", "admin")


async def test_api_key_verifies_and_expires(db, rows):
    user = await rows.user()
    key, raw = await cr.issue_api_key(db, user, name="laptop")
    assert key.kind == "api" and key.scope == "llm" and key.key_hash != raw
    found = await cr.verify_api_key(db, raw)
    assert (found.user_id, found.kind) == (user.id, "api")

    key.expires_at = utcnow() - timedelta(seconds=1)
    await db.flush()
    with pytest.raises(CredentialError) as exc:
        await cr.verify_api_key(db, raw)
    assert exc.value.code == "expired_api_key"

    key.expires_at = None
    key.revoked_at = utcnow()
    await db.flush()
    with pytest.raises(CredentialError) as exc:
        await cr.verify_api_key(db, raw)
    assert exc.value.code == "invalid_api_key"


async def test_key_of_a_waiting_or_disabled_account_is_refused(db, rows):
    user = await rows.user()
    _key, raw = await cr.issue_api_key(db, user, name="k")
    user.approval_pending = True
    await db.flush()
    with pytest.raises(CredentialError):
        await cr.verify_api_key(db, raw)
    user.approval_pending = False
    user.disabled_at = utcnow()
    await db.flush()
    with pytest.raises(CredentialError):
        await cr.verify_api_key(db, raw)


async def _env(db, rows, state="running", kind="booking"):
    user = await rows.user()
    s = orm.DevSession(kind=kind, user_id=None if kind == "shared" else user.id, image="img", state=state)
    db.add(s)
    await db.flush()
    return user, s


async def test_env_key_lives_with_its_environment(db, rows):
    user, s = await _env(db, rows, state="starting")
    raw = await cr.issue_env_key(db, s.id, until=utcnow() + timedelta(hours=2))
    assert raw.startswith("sk-ss-env-")
    found = await cr.verify_api_key(db, raw)
    assert (found.user_id, found.kind, found.scope, found.devenv_session_id) == (user.id, "env", "llm", s.id)

    s.state = "stopping"
    await db.flush()
    with pytest.raises(CredentialError) as exc:
        await cr.verify_api_key(db, raw)
    assert exc.value.code == "invalid_api_key"

    s.state = "running"
    await db.flush()
    assert await cr.revoke_env_keys(db, s.id) == 1
    with pytest.raises(CredentialError):
        await cr.verify_api_key(db, raw)


async def test_env_key_is_capped_and_reissued(db, rows):
    _user, s = await _env(db, rows)
    first = await cr.issue_env_key(db, s.id, until=utcnow() + timedelta(days=3))
    key = (await db.execute(orm.ApiKey.__table__.select())).first()
    assert key.expires_at - utcnow() <= timedelta(hours=cr.ENV_KEY_MAX_HOURS)
    second = await cr.issue_env_key(db, s.id)  # a restart: the old key ends
    with pytest.raises(CredentialError):
        await cr.verify_api_key(db, first)
    await cr.verify_api_key(db, second)
    assert await cr.extend_env_keys(db, s.id, utcnow() + timedelta(hours=1)) == 1


async def test_shared_environment_gets_no_key(db, rows):
    _user, s = await _env(db, rows, kind="shared")
    with pytest.raises(CredentialError) as exc:
        await cr.issue_env_key(db, s.id)
    assert exc.value.code == "env_key_shared"


async def test_invite_uses_expiry_and_revocation(db, rows):
    admin = await rows.user(is_admin=True)
    invite, raw = await cr.issue_invite(db, admin, days=7, max_uses=2)
    assert raw.startswith("ssi-") and invite.token_hash == cr.digest(raw)
    assert (await cr.check_invite(db, raw)).id == invite.id
    assert await cr.consume_invite(db, invite.id)
    assert await cr.consume_invite(db, invite.id)
    assert not await cr.consume_invite(db, invite.id)  # used up
    await db.refresh(invite)
    assert cr.invite_state(invite) == "used_up"
    with pytest.raises(CredentialError) as exc:
        await cr.check_invite(db, raw)
    assert exc.value.code == "invite_used_up"

    other, raw2 = await cr.issue_invite(db, admin)
    other.expires_at = utcnow() - timedelta(minutes=1)
    await db.flush()
    with pytest.raises(CredentialError) as exc:
        await cr.check_invite(db, raw2)
    assert exc.value.code == "invite_expired"
    assert not await cr.consume_invite(db, other.id)

    third, raw3 = await cr.issue_invite(db, admin)
    await cr.revoke_invite(db, third)
    await db.flush()
    with pytest.raises(CredentialError) as exc:
        await cr.check_invite(db, raw3)
    assert exc.value.code == "invite_revoked"

    for bad in ("", "change-me", "ssi-unknown", "sk-ss-xyz"):
        with pytest.raises(CredentialError) as exc:
            await cr.check_invite(db, bad)
        assert exc.value.code == "invite_invalid"


async def test_invite_limits(db, rows):
    admin = await rows.user(is_admin=True)
    for days, uses in ((0, 1), (cr.INVITE_MAX_DAYS + 1, 1), (1, 0), (1, cr.INVITE_MAX_USES + 1)):
        with pytest.raises(ValueError):
            await cr.issue_invite(db, admin, days=days, max_uses=uses)


def test_playground_rate_limiter(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("slotserve.api.playground.time.monotonic", lambda: now[0])
    limiter = RateLimiter()
    assert all(limiter.hit(1, 3) is None for _ in range(3))
    assert limiter.hit(1, 3) == pytest.approx(60.0)
    assert limiter.hit(2, 3) is None  # per person
    assert limiter.hit(1, 0) is None  # 0 = no limit
    now[0] += 61
    assert limiter.hit(1, 3) is None

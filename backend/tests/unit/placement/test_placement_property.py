"""A03 property: booking what ``placement.fit`` offers is always accepted ("recommended but refused" = 0).

Random worlds (lanes, people, models, templates, kept environments, existing bookings) and random settings (quotas,
approval, limits, dev environments on/off) meet random requests. Every candidate ``fit`` returns is then booked
the way QuickBook books it - through the real booking code, on the candidate's lane, in the window ``fit``
returned - and the outcome must match: a fitting candidate is accepted, any other one is refused with one of the
reasons ``fit`` gave. The unit layer runs a few hundred cases; ``SLOTSERVE_PLACEMENT_CASES=1000`` runs more.
"""

import os
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from slotserve import orm
from slotserve.config import Settings
from slotserve.db import utcnow
from slotserve.errors import ApiError
from slotserve.schemas import ReservationCreateIn
from slotserve.services import placement

CASES = int(os.environ.get("SLOTSERVE_PLACEMENT_CASES", "100"))
REQUESTS_PER_WORLD = 12
H = timedelta(hours=1)
M = timedelta(minutes=1)


def _booking_module():
    """Where ``create_core`` lives (it moves from api/ to services/ in W1-03)."""
    try:
        from slotserve.services import reservations as mod  # type: ignore[attr-defined]

        if hasattr(mod, "create_core"):
            return mod
    except ImportError:
        pass
    from slotserve.api import reservations as mod

    return mod


APP = SimpleNamespace(
    state=SimpleNamespace(
        notifier=SimpleNamespace(emit=lambda *a, **k: None), lanes=SimpleNamespace(poke=lambda *a: None)
    )
)


def random_settings(rnd: random.Random) -> Settings:
    return Settings.model_construct(
        quota_weekly_hours=rnd.choice([0, 0, 0, 3, 12]),
        quota_upcoming=rnd.choice([0, 0, 0, 1, 4]),
        approval_mode=rnd.choice(["off", "long", "all"]),
        approval_hours=rnd.choice([1, 4]),
        max_reservation_hours=rnd.choice([3, 8]),
        max_advance_days=rnd.choice([2, 30, 30]),
        devenv_enabled=rnd.random() < 0.9,
        devenv_projects=rnd.choice([0, 1, 3, 3]),
        devenv_password_login=rnd.random() < 0.5,
    )


async def build_world(db, rnd: random.Random, now):
    tables = (orm.Reservation, orm.DevProject, orm.SshKey, orm.DevTemplate, orm.Lane, orm.ModelPreset, orm.LlmModel)
    for table in (*tables, orm.User):
        await db.execute(delete(table))
    await db.commit()
    db.expunge_all()
    users = [
        orm.User(
            username=f"user{i}",
            password_hash="x",
            is_admin=i == 0 and rnd.random() < 0.5,
            devenv_password_hash="h" if rnd.random() < 0.4 else None,
        )
        for i in range(3)
    ]
    db.add_all(users)
    lanes = [
        orm.Lane(name=f"lane{i}", gpu_indices=rnd.choice(["", str(i), f"{i},{i + 4}"]), position=rnd.randint(0, 3))
        for i in range(rnd.randint(1, 3))
    ]
    db.add_all(lanes)
    models = [
        orm.LlmModel(name="ready", file_path="/m/ready.gguf", status="ready"),
        orm.LlmModel(name="busy", file_path="/m/busy.gguf", status="downloading"),
    ]
    db.add_all(models)
    await db.flush()
    lanes[0].default_model_id = models[0].id
    preset = orm.ModelPreset(model_id=models[0].id, name="long")
    db.add(preset)
    for u in users:
        if rnd.random() < 0.8:
            db.add(orm.SshKey(user_id=u.id, name="k", public_key="ssh-ed25519 AAAA", fingerprint=f"fp{u.id}"))
    templates = [
        orm.DevTemplate(name="pytorch", image="pt", kind="builtin", enabled=True),
        orm.DevTemplate(name="off", image="off", kind="builtin", enabled=False),
        orm.DevTemplate(name="mine", image="mine", kind="custom", owner_id=users[1].id),
    ]
    db.add_all(templates)
    await db.flush()
    projects = []
    for u in users:
        for j in range(rnd.randint(0, 2)):
            projects.append(orm.DevProject(owner_id=u.id, name=f"p{u.id}-{j}", template_id=templates[0].id, image="pt"))
    db.add_all(projects)
    await db.flush()
    slot = timedelta(minutes=30)
    base = now.replace(minute=0, second=0, microsecond=0)
    for _ in range(rnd.randint(0, 6)):
        start = base + slot * rnd.randint(-8, 60)
        project = rnd.choice(projects) if projects and rnd.random() < 0.3 else None
        db.add(
            orm.Reservation(
                user_id=project.owner_id if project else rnd.choice(users).id,
                lane_id=rnd.choice(lanes).id,
                start_at=start,
                end_at=start + slot * rnd.randint(1, 8),
                status=rnd.choice(["booked", "booked", "pending", "cancelled"]),
                devenv_project_id=project.id if project else None,
                devenv_template_id=project.template_id if project else None,
            )
        )
    await db.commit()
    # ids only: a refused booking rolls the session back, which expires the objects
    return SimpleNamespace(
        users=[u.id for u in users],
        lanes=[x.id for x in lanes],
        models=[m.id for m in models],
        preset=preset.id,
        templates=[x.id for x in templates],
        projects=[x.id for x in projects],
    )


def random_request(rnd: random.Random, w, now) -> tuple[placement.Request, tuple]:
    kind = rnd.choice(["model", "exclusive", "devenv", "devenv"])
    req = placement.Request(user_id=rnd.choice(w.users), kind=kind)
    if kind == "model":
        req.model_id = rnd.choice([w.models[0], w.models[0], w.models[1], 9999])
        req.preset_id = w.preset if rnd.random() < 0.2 else None
    elif kind == "devenv":
        if w.projects and rnd.random() < 0.4:
            req.devenv_project_id = rnd.choice(w.projects)
        else:
            req.devenv_template_id = rnd.choice(w.templates)
            if rnd.random() < 0.4:
                req.devenv_project_name = rnd.choice(["thesis", "  "])
    if rnd.random() < 0.25:
        req.lane_id = rnd.choice(w.lanes)
    start = now + rnd.choice([-45 * M, -10 * M, timedelta(0), timedelta(0), 20 * M, 2 * H, 26 * H, timedelta(days=3)])
    length = rnd.choice([3 * M, 30 * M, H, H, 2 * H, 2 * H, 4 * H, 9 * H])
    return req, (start, start + length)


def body_for(req: placement.Request, cand: placement.Candidate, window) -> ReservationCreateIn:
    """The booking QuickBook sends for a candidate."""
    return ReservationCreateIn(
        model_id=req.model_id if req.kind == "model" else None,
        preset_id=req.preset_id if req.kind == "model" else None,
        start_at=window[0],
        end_at=window[1],
        lane_id=cand.lane_id,
        devenv_template_id=req.devenv_template_id if req.kind == "devenv" and req.devenv_project_id is None else None,
        devenv_project_id=req.devenv_project_id if req.kind == "devenv" else None,
        devenv_project_name=req.devenv_project_name if req.kind == "devenv" else None,
    )


@pytest.mark.parametrize("seed", [1, 2])
async def test_fitting_candidates_are_always_accepted(db, monkeypatch, seed):
    mod = _booking_module()
    rnd = random.Random(seed)
    current = {"s": Settings.model_construct()}
    monkeypatch.setattr(mod, "get_settings", lambda: current["s"])
    stats = {"accepted": 0, "refused": 0}
    world = None
    for case in range(CASES // 2):
        now = utcnow()
        if case % REQUESTS_PER_WORLD == 0:
            world = await build_world(db, rnd, now)
        s = random_settings(rnd)
        current["s"] = s
        req, window = random_request(rnd, world, now)
        result = await placement.evaluate(db, req, window, now, settings=s)
        assert [c.fits for c in result.candidates] == sorted([c.fits for c in result.candidates], reverse=True), (
            "fitting candidates come first"
        )
        for cand in result.candidates:
            user = await db.get(orm.User, req.user_id)
            body = body_for(req, cand, result.window)
            try:
                created = await mod.create_core(APP, db, user, body)
            except ApiError as err:
                await db.rollback()
                code = err.detail["code"]
                assert not cand.fits, (
                    f"case {case}: fit offered lane {cand.lane_id} for {req} {window}, refused: {code}"
                )
                assert code in {r.code for r in cand.reasons_no}, (code, cand.reasons_no)
                stats["refused"] += 1
                continue
            assert cand.fits, f"case {case}: accepted though fit said no: {cand.reasons_no} ({req})"
            assert created[0].lane_id == cand.lane_id
            assert (created[0].status == "pending") == cand.needs_approval
            stats["accepted"] += 1
            project_id = created[0].devenv_project_id if req.devenv_project_name else None
            for r in created:
                await db.delete(r)
            if project_id is not None and project_id != req.devenv_project_id:
                await db.execute(delete(orm.DevProject).where(orm.DevProject.id == project_id))
            await db.commit()
    # the generator has to reach both outcomes often, or the property says little
    assert stats["accepted"] > CASES // 10 and stats["refused"] > CASES // 10, stats

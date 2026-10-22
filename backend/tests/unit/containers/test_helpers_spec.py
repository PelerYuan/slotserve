"""Helper containers (R1-06, arch3-2 K2-1): SlotServe's image, as little of a storage as possible, seeds without
running the environment's image."""

from slotserve.containers import helpers
from slotserve.containers.harden import check


def test_parent_binds_never_the_root():
    rels = ["users/u1", "users/u2", "projects/p7/root-.cache"]
    assert helpers.parent_binds("/srv/st/", rels, "/s") == [
        "/srv/st/users:/s/users",
        "/srv/st/projects/p7:/s/projects/p7",
    ]


def test_prepare_script_reports_new_folders():
    folders = [helpers.Folder("/s/users/u1", seed="/workspace"), helpers.Folder("/s/users/it's", owner=20002)]
    script = helpers.prepare_script(folders)
    assert "if [ ! -d '/s/users/u1' ]; then mkdir -p '/s/users/u1'; echo \"==NEW== 0\"; fi" in script
    assert "chown 20002:20002 '/s/users/it'''s' && chmod 700" in script
    assert "cp " not in script  # nothing of the environment's image runs or is read here
    assert helpers.new_folders("noise\n==NEW== 0\n==NEW== 1\n") == {0, 1}
    seed = helpers.seed_script([(0, folders[0])])
    assert seed == (
        "if [ -d '/tmp/slotserve-seed/0/workspace' ]; then"
        " cp -a '/tmp/slotserve-seed/0/workspace/.' '/s/users/u1'/ || true; fi"
    )


def test_helper_and_seed_source_are_hardened():
    body = helpers.helper_body(
        helpers.helper_spec("busybox:1.37", "true", label_key="slotserve.storage", binds=["/a/b:/s/b"])
    )
    assert check(body) == [] and body["Image"] == "busybox:1.37" and body["User"] == "root"
    src = helpers.helper_body(helpers.seed_source_spec("person/image:1", label_key="slotserve.storage"))
    host = src["HostConfig"]
    assert check(src) == [] and src["User"] == "65534:65534" and host["NetworkMode"] == "none"
    assert "Mounts" not in host and "Binds" not in host

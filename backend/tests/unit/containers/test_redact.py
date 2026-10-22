"""Preview and diagnostics never show secrets (A01 acceptance 5)."""

from slotserve.containers.redact import MASK, redact


def test_by_name_and_by_value():
    token = "s3cret-web-token"
    body = {
        "Env": ["SLOTSERVE_WEB_TOKEN=" + token, "JUPYTER_TOKEN=" + token, "HASHED_PASSWORD=abc123", "PLAIN=x"],
        "Cmd": ["/bin/sh", "-c", f"jupyter lab --IdentityProvider.token={token}"],
        "Options": {"o": "addr=nas,username=svc,password=smb-pass,vers=3.0"},
        "password": "whatever",
        "files": {"opt/slotserve/root.hash": b"$6$salt$hash"},
    }
    out = redact(body, [token, "smb-pass", b"hostkeybytes", None, ""])
    assert out["Env"] == [f"SLOTSERVE_WEB_TOKEN={MASK}", f"JUPYTER_TOKEN={MASK}", f"HASHED_PASSWORD={MASK}", "PLAIN=x"]
    assert token not in str(out) and "smb-pass" not in str(out) and "$6$" not in str(out)
    assert out["password"] == MASK and out["files"]["opt/slotserve/root.hash"] == MASK
    assert body["Env"][0].endswith(token)  # the input is untouched

"""Preview and diagnostics never show secrets (A01 acceptance 5): a container body through ``redact_mapping``."""

from slotserve.containers.redact import MASK, redact_mapping


def test_container_body():
    token = "s3cret-web-token"
    body = {
        "Env": ["SLOTSERVE_WEB_TOKEN=" + token, "JUPYTER_TOKEN=" + token, "HASHED_PASSWORD=abc123", "PLAIN=x"],
        "Cmd": ["/bin/sh", "-c", f"jupyter lab --IdentityProvider.token={token}"],
        "Options": {"o": "addr=nas,username=svc,password=smb-pass,vers=3.0"},
        "root": "$6$goldensalt$alicehashalicehash",
    }
    out = redact_mapping(body, extra=[token, "smb-pass"])
    assert out["Env"][3] == "PLAIN=x" and all(MASK in e for e in out["Env"][:3])
    assert token not in str(out) and "smb-pass" not in str(out) and "$6$" not in str(out) and "abc123" not in str(out)
    assert body["Env"][0].endswith(token)  # the input is untouched

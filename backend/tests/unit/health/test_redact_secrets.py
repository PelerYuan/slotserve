"""redact(): nothing that is a secret survives "copy diagnostics" / doctor output (CP-B)."""

from slotserve.containers.redact import MASK, redact, redact_mapping

SECRETS = {
    "secret_key": "s3cr3t-key-value-0123456789abcdef",
    "invite_code": "join-us-please-42",
    "metrics_token": "metrics-tok-777777",
    "telegram_bot_token": "123456789:AAEhBP0av28UVbQ1i2pSxQaXzyR7u2kCZ1x",
    "feishu_app_secret": "feishu-app-secret-xyz",
    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab",
    "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=ddddeeeeffff0000111122223333",
    "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7xxx-4bc4-97a0-0ec2sifa5aaa",
    "hf_token": "hf_abcdefghijklmnopqrstuvwxyz012345",
}


def test_secret_settings_are_removed(settings):
    s = settings(**SECRETS)
    text = "\n".join(f"{k} is {v}" for k, v in SECRETS.items())
    out = redact(text, s)
    for value in SECRETS.values():
        assert value not in out
    assert MASK in out


def test_credential_shapes_are_removed_without_settings():
    samples = {
        "Authorization: Bearer abcdefghijklmnop0123": "abcdefghijklmnop0123",
        "cookie: slotserve_session=eyJ1aWQiOjF9.Zz9xYz.sig": "eyJ1aWQiOjF9",
        "set-cookie: devenv_web=tok123456; Path=/": "tok123456",
        "used key sk-ss-AbCdEfGhIjKlMnOpQrStUvWx in a request": "sk-ss-AbCdEfGhIjKlMnOpQrStUvWx",
        "mount -o username=bob,password=hunter2pass,vers=3.0": "hunter2pass",
        "smb://bob:hunter3pass@nas/share": "hunter3pass",
        "GET /cb?access_token=qwertyuiop123&x=1": "qwertyuiop123",
        "SLOTSERVE_SECRET_KEY=abc123def456ghi": "abc123def456ghi",
        "NODE_TOKEN='node-token-value-99'": "node-token-value-99",
        '{"wecom_aes_key": "aeskey-0000111122223333"}': "aeskey-0000111122223333",
        "root:$6$saltsalt$HashHashHashHashHashHash:19000:0:99999:7:::": "HashHashHashHashHashHash",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----": (
            "b3BlbnNzaC1rZXktdjEAAAAA"
        ),
    }
    for text, secret in samples.items():
        out = redact(text)
        assert secret not in out, (text, out)


def test_ordinary_text_is_kept():
    text = "lane gpu0 failed: CUDA out of memory (port 54029); 3 bookings, model qwen2.5-7b"
    assert redact(text) == text


def test_mappings_mask_secret_keys_and_values(settings):
    s = settings(**SECRETS)
    data = {
        "settings": {"hf_token": "anything-at-all", "timezone": "UTC", "devenv_password_login": True},
        "log": [f"posting to {SECRETS['feishu_webhook_url']}"],
        "headers": {"Authorization": "Bearer zzzzzzzzzzzz"},
    }
    out = redact_mapping(data, s)
    assert out["settings"]["hf_token"] == MASK and out["settings"]["timezone"] == "UTC"
    assert out["settings"]["devenv_password_login"] is True
    assert SECRETS["feishu_webhook_url"] not in str(out) and "zzzzzzzzzzzz" not in str(out)

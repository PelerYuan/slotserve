# Security Policy

## Supported versions

Security fixes are made on the latest release only.

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's [private vulnerability reporting](https://github.com/PelerYuan/slotserve/security/advisories/new) instead. Include steps to reproduce and the impact you expect. You will get a reply within a week.

## Deployment notes

SlotServe is designed for trusted local networks. When exposing it more widely:

- set strong, unique `SLOTSERVE_INVITE_CODE` and `SLOTSERVE_SECRET_KEY` values;
- serve it over HTTPS (reverse proxy) and set `SLOTSERVE_COOKIE_SECURE=true`;
- keep `SLOTSERVE_LLAMA_HOST=127.0.0.1` so llama-server is only reachable through the gateway;
- remember that model launch arguments are passed to `llama-server` - only give accounts to people you trust.

# Operations

Runbooks for the people who keep a SlotServe installation running: upgrading, backups and restore, the first admin,
reverse proxies, health checks and alerts, container hardening, firewall ports and checking an installation on real
hardware. Each problem runbook reads **symptom → diagnose → fix → prevent**.

The commands assume the Docker Compose installation from [Getting started](getting-started.md), run in the folder
with `docker-compose.yml`. Without Docker, drop the `docker compose exec slotserve` prefix and run `slotserve ...`
as the user that runs SlotServe, with the same environment (`.env`).

| Situation | Go to |
| --- | --- |
| Moving from v0.8 to v0.10 | [Upgrade from v0.8](#upgrade) |
| An upgrade went wrong | [Upgrade failed: roll back](#rollback) |
| Setting up or checking backups | [Backups](#backups), [The secret key](#secret-key) |
| Putting a backup back, moving to another machine | [Restore from a backup](#restore), [Move to a new machine](#new-machine) |
| Nobody can sign in as admin | [First admin and setup code](#setup-code), [Forgotten admin password](#admin-password) |
| Running behind nginx, Caddy or Traefik | [Reverse proxy](#reverse-proxy) |
| Something is red on Admin → Overview | [Health checks and `slotserve doctor`](#health), [Inbox and alerts](#alerts) |
| `sudo` or an app does not work in an environment | [Container hardening and compatibility mode](#hardening) |
| Which ports to open | [Ports and firewall](#firewall) |
| Disk full, GPUs gone, environments do not start, a leaked password | [Runbooks](#runbooks) |
| Checking an installation before people use it | [Verify on real hardware](#verify) |

## Upgrade from v0.8 {#upgrade}

v0.10 changes how people sign in, how dev environments are isolated and how data is backed up. Most of it needs
nothing from you; this section lists what people will notice and what to check. The full list is under **Breaking
changes** in `CHANGELOG.md`.

### Before you upgrade {#upgrade-before}

1. Read the **Breaking changes** of the release.
2. Make a backup you can find again: **Admin → Overview → Backups → Back up now**, or stop SlotServe and copy the
   whole data directory (`DATA_DIR`, default `./data`). The upgrade also takes its own snapshot (below).
3. Note your image tag. `docker-compose.yml` now follows a minor version (`ghcr.io/peleryuan/slotserve:0.10`, which
   gets 0.10.x fixes) instead of `latest`. If your `.env` sets `SLOTSERVE_IMAGE`, change it to the new version.
4. Tell people about the one-time sign-in and the SSH host key warning below. Running dev environments keep running
   while SlotServe restarts; the new container settings apply on their next start.

### Upgrade {#upgrade-steps}

```bash
git pull                      # the new docker-compose.yml and docs
docker compose pull           # or: docker compose up -d --build
docker compose up -d
docker compose logs -f slotserve
```

On the first start the server:

- saves a **snapshot** before it changes the database: `data/backups/slotserve-v<old>-to-v<new>-<time>/` with the
  database, `secret_key` and the dev environment SSH host keys. Snapshots are never deleted automatically;
- applies the numbered database migrations **all or nothing** (a failure rolls back and the server does not start;
  see [Roll back](#rollback)) and writes `data/migration_report.json`;
- generates a secret key in `data/secret_key` if `SLOTSERVE_SECRET_KEY` is unset or still `change-me` (see
  [The secret key](#secret-key)).

`reset-password` and `set-admin` refuse to run on a database that still needs upgrading: start the server once
first.

### What people notice {#upgrade-after}

**Everyone signs in once more.** Browser sign-ins are now stored on the server, so all old sign-ins end with the
upgrade. API keys keep working unchanged. Changing your password now signs out your other browsers.

**SSH warns about a changed host key, once per environment.** Every dev environment now has its own SSH host key
(before, all environments shared one that anyone could read inside their container). Existing environments get
their key on their next start, and the first `ssh` afterwards stops with `REMOTE HOST IDENTIFICATION HAS CHANGED`.
That is expected exactly once. Tell people to:

1. compare the fingerprint `ssh` prints with the **Host key** shown on the environment's card (Dev environments);
2. remove the old entry and connect again:

    ```bash
    ssh-keygen -R "[server]:2201"     # the host and port of the environment's ssh command
    ```

A warning **after** that first time, for an environment whose key has not changed on its card, is a real warning:
do not ignore it. Kept environments created by an older version are snapshotted and recreated once on their next
start (that start takes longer), because the new settings cannot be added to an existing container.

**Shared environments of idle lanes need Join.** They accept SSH keys only, and only people who joined them get
an account (Dev environments → **Join**). Everyone who had an SSH key at the upgrade was joined to every lane.

**Containers are hardened.** `sudo` and other setuid programs stop working inside dev environments, `ping` does
not work, and a fork bomb or out-of-memory job stays inside its container. A memory limit of 0 now means "the
host's memory less max(4 GB, 10 %)" instead of "no limit". A lane that names no GPUs cannot start a GPU
environment any more: give the lane its GPUs in **Admin → GPUs & lanes**. Templates that really need `sudo` or
their own init system run in compatibility mode; see [Container hardening](#hardening).

**Sign-ups.** Installations that already have an admin are not asked for a setup code. While the invite code is
still `change-me`, nobody can sign up: set an invite code or hand out invite links (Admin → Users).

**Behind a reverse proxy**, changes in the web UI may be refused with "did not come from a SlotServe page" until
the proxy passes the `Host` header or `SLOTSERVE_PUBLIC_URL` is set, and sign-in throttling needs
`SLOTSERVE_FORWARDED_ALLOW_IPS`. See [Reverse proxy](#reverse-proxy).

**Firewall.** Dev environment web interfaces (VS Code, JupyterLab, the desktop) are served on their own port,
8001 by default. Open it if people use them; see [Ports and firewall](#firewall).

### Check the upgrade {#upgrade-check}

1. **Admin → Overview**: the setup checklist and the system health table. Every check should be OK or a note you
   understand; see [Health checks](#health).
2. `docker compose exec slotserve slotserve doctor` gives the same list on the command line.
3. `data/migration_report.json` lists every migration step; the health check **migration** warns when an upgrade
   could not keep some rows.
4. Sign in, open a dev environment in the browser, call a model in the Playground.

### Upgrade failed: roll back {#rollback}

**Symptom.** The server does not come up after an upgrade, or it runs but something you rely on is broken.

**Diagnose.**

- `docker compose logs slotserve` shows why. A failed migration says `database upgrade failed and was rolled back;
  the snapshot is in ...`: the database is unchanged and still the old version's.
- An older version started on an upgraded database refuses to start with `... was used by a newer SlotServe`.
  This protects your data; it is not the problem itself.

**Fix.** To run the previous version again:

```bash
docker compose stop slotserve
ls -d data/backups/slotserve-v*-to-v*          # the newest is the snapshot of this upgrade
SNAP=data/backups/slotserve-v...-to-v...-<time>   # the folder name from ls
cp "$SNAP/slotserve.db" data/slotserve.db
rm -f data/slotserve.db-wal data/slotserve.db-shm
[ -f "$SNAP/secret_key" ] && cp "$SNAP/secret_key" data/secret_key
[ -d "$SNAP/devenv" ] && cp -r "$SNAP/devenv" data/
```

Then pin the old version in `.env` and start it:

```bash
echo "SLOTSERVE_IMAGE=ghcr.io/peleryuan/slotserve:0.8.0" >> .env
docker compose up -d
```

Everything that happened between the upgrade and the rollback (bookings, keys, settings) is lost with the newer
database; keep `data/slotserve.db` somewhere before overwriting it if you may need it. When the migration itself
failed (the database was not changed), only pinning the old version is needed.

**Prevent.** Rehearse the upgrade on a copy of the data directory first (another folder as `DATA_DIR`, another
`PORT`, dev environments off). Keep the image pinned to a minor version and upgrade on purpose. Report a failed
migration with `data/migration_report.json` and the log.

## Backups {#backups}

### What a backup contains {#backup-contents}

Every day at `backup_hour` (default 3:00 in the SlotServe time zone; `-1` = every 24 hours from the last one)
SlotServe writes a **bundle**, a folder `data/backups/slotserve-daily-<date>-<time>/` with:

| File | What it is |
| --- | --- |
| `slotserve.db` | A consistent copy of the database (SQLite's online backup, safe while the server runs) |
| `secret_key` | The generated [secret key](#secret-key); absent when the key comes from `SLOTSERVE_SECRET_KEY` |
| `devenv/` | The dev environments' SSH host keys |
| `manifest.json` | Version, schema, the key's fingerprint (never the key), sizes, checksums, row counts, the storages and kept environments, and what the bundle does not contain |

**Admin → Overview → Backups** lists the bundles, makes one on demand (**Back up now**, a `manual` bundle) and
downloads the database file. The newest `backup_keep` (default 7) daily and manual bundles are kept; upgrade
snapshots and `prerestore` bundles are never removed automatically. `backup_keep` 0 switches automatic backups off.

**Not in a backup**: `/workspace` and other kept directories, kept environments' containers, saved environment
images, models, and the data on managed storages. Back those up separately where they live:

- **Storages** (Admin → Storage: host folders, NFS, SMB): with the backup of that server or folder.
- **Docker volumes** (when no storage and no storage directory is set), e.g. one person's `/workspace`:

    ```bash
    docker run --rm -v slotserve-workspace-u3:/v:ro -v "$PWD":/out busybox:1.37 \
      tar czf /out/workspace-u3.tgz -C /v .
    ```

- **Models**: usually downloaded again, or copied with the models folder.
- **Saved images**: `docker save` the images you care about.

### Keep a copy off the machine {#backup-target}

A backup on the same disk does not survive the disk. Set **Copy backups to** (`backup_target`, Admin → System
settings) and every daily bundle is copied there after it is written, into a folder `slotserve-backups/`, keeping
the newest `backup_keep` copies:

- **An absolute path** SlotServe can write, typically a mounted NAS share. With Docker this is a path **inside the
  container**: mount the share into it first, e.g. in `docker-compose.yml` under `volumes`:

    ```yaml
    - /mnt/nas/slotserve:/backup-target
    ```

    and set `backup_target` to `/backup-target`.
- **`storage:<id>`**: a storage from **Admin → Storage** (its id is in the list). SlotServe writes the bundle
  through a short-lived helper container, so the share does not need to be mounted into SlotServe.

The setup checklist item **Backups kept elsewhere** ticks once a bundle has been copied.

### When a backup fails {#backup-alerts}

The health checks **backup** and **backup_age** watch the backups, and admins are alerted (inbox, then chat
platforms) when:

- the last backup failed (the evidence has the error; **Try again** makes a new one);
- the copy to the backup target failed or the target is not valid;
- the newest backup is older than 36 hours (the server was down, or the backup task is stuck).

A warning "Backups are only on this machine's disk" stays until a backup target is set (it can be dismissed with a
reason). `/metrics` exports `slotserve_backup_age_seconds` for external monitoring.

### The secret key {#secret-key}

The secret key signs browser sessions and encrypts the secrets SlotServe stores: storage passwords (SMB) and the
viewable copies of API keys (when **Let people view their API keys again** is on). It does not protect the API keys themselves (they are
stored as hashes) or passwords (also hashes).

- When `SLOTSERVE_SECRET_KEY` is **unset, empty or `change-me`**, SlotServe generates a random key on the first
  start and keeps it in `data/secret_key` (readable by its owner only). Every backup bundle includes it.
- When `SLOTSERVE_SECRET_KEY` is **set** to anything else, that value is used and is **not** written into backups:
  keep it with your other secrets (a password manager), next to the `.env`.

**What to back up**: the backup bundles (copied off the machine), plus `.env` and, if you set it, the secret key.
Copying the whole data directory while SlotServe is stopped is also a complete backup of SlotServe's own state.

#### Secret key lost or changed {#secret-key-lost}

**Symptom.** After a restart or a move everyone is signed out; the health check **secrets** says "N stored
secret(s) cannot be decrypted with the current secret key"; SMB storages fail to mount; viewable API key copies
cannot be shown.

**Diagnose.** The check's evidence lists what cannot be decrypted. `slotserve doctor` shows the same. Compare the
key's fingerprint in a bundle's `manifest.json` (`secret_key.fingerprint`) between a bundle from before and one
from after the change.

**Fix.**

- **Put the old key back** if you have it: the `secret_key` file of a bundle from before the change (stop
  SlotServe, copy it to `data/secret_key`, start), or the old value of `SLOTSERVE_SECRET_KEY` in `.env`. An
  explicitly set `SLOTSERVE_SECRET_KEY` always wins over `data/secret_key`: remove it from `.env` if you want the
  file to be used.
- **Without the old key**, enter the listed secrets again: the password of each SMB storage in **Admin →
  Storage**; viewable API key copies cannot be recovered (the keys themselves keep working; their owners can make
  new ones). People sign in again.

**Rotating the key on purpose** (it leaked): stop SlotServe, set a new `SLOTSERVE_SECRET_KEY` (`openssl rand -hex
32`) or delete `data/secret_key` to have a new one generated, start, then re-enter the secrets as above. Everyone is
signed out.

**Prevent.** Keep backups off the machine; if you set the key yourself, store it outside the server.

## Restore from a backup {#restore}

`slotserve restore` puts a bundle back. The server must be stopped.

```bash
docker compose stop slotserve
docker compose run --rm --no-deps slotserve slotserve restore slotserve-daily-20261020-030000
docker compose up -d
```

- The argument is a bundle folder name in `data/backups/` or a path the command can see (inside the container: put
  a bundle from the NAS into `DATA_DIR/backups/` first).
- It asks you to type `restore` (`--yes` skips the question). It refuses a backup from a newer SlotServe and one
  that fails SQLite's integrity check.
- Before it replaces anything, the current state is saved as a `slotserve-prerestore-<time>` bundle, so a restore
  can itself be undone the same way.
- It puts back the database, the secret key (`--keep-key` keeps the current one; secrets of the backup then cannot
  be decrypted) and the SSH host keys. A backup from an older version is upgraded on the next start, after its own
  snapshot. The command prints warnings you must act on, for example that `SLOTSERVE_SECRET_KEY` in `.env`
  overrides the restored key.

Older single-file backups (`slotserve-daily-<time>.db`) and upgrade snapshots can be restored the same way.

### The reconciliation report {#reconcile}

The restored database describes the moment of the backup, while Docker and the disk have moved on. On the next
start SlotServe compares them and shows the result as the health check **restore** on **Admin → Overview** (also
in `data/restore_report.json`):

| Finding | Meaning | What to do |
| --- | --- | --- |
| Environment missing | The database expects a container that Docker does not have (removed after the backup, or a new machine) | For a kept environment, its installed software is gone; its kept directories are not affected. End or delete it, and start it again from its template. |
| Unknown container | A SlotServe container the database does not know (created after the backup) | Save what matters from it (`docker cp`, or let its owner copy files out), then remove it with `docker rm -f <name>`. |
| Model file missing | A model whose file is not where the database says | Copy the file back, or scan the models folder and remove the stale entry. |
| Storages | Listed for a re-check | Press **Check** for each in **Admin → Storage**. |

Volumes and ports are not compared. When Docker is not reachable the check waits ("waiting for Docker to
compare") and runs once Docker is back.

**Until an admin dismisses the restore check, SlotServe removes nothing the database does not know** (no cleanup
of unknown containers, networks or volumes). Read the report, deal with each line, then **Dismiss** it with a
reason; normal cleanup resumes.

### Move to a new machine {#new-machine}

1. Install SlotServe on the new machine as in [Getting started](getting-started.md), with the **same or a newer**
   version, and copy `.env` (it holds `SLOTSERVE_SECRET_KEY` if you set one). Do not start it yet.
2. Copy the newest bundle into the new `DATA_DIR/backups/` and restore it:

    ```bash
    docker compose run --rm --no-deps slotserve slotserve restore --yes slotserve-daily-20261020-030000
    ```

3. Copy what is not in the bundle: the models folder, Docker volumes or the storage directory with `/workspace`
   and kept directories, saved images (`docker save` / `docker load`). Storages on a NAS only need the new machine
   to reach them (NFS exports allow its address; `nfs-common` / `cifs-utils` installed).
4. Start (`docker compose up -d`) and read the [reconciliation report](#reconcile): environments of the old machine
   are listed as missing.
5. Point people at the new address (`SLOTSERVE_PUBLIC_URL`, DNS). Everyone's SSH known_hosts entries change with
   the address; the host keys themselves came with the bundle.

## First admin and setup code {#setup-code}

A new installation has no admin. Until one exists, SlotServe prints a one-time **setup code** in its log at every
start; the sign-up form asks for it instead of the invite code, and that account becomes the admin:

```bash
docker compose logs slotserve | grep -A4 "No admin account"
docker compose exec slotserve slotserve setup-code
```

The code stops working as soon as an admin exists, and installations that already have an admin never ask for it.
While the invite code is still `change-me`, nobody else can sign up: set **Invite code** in Admin → System
settings, or create **invite links** in Admin → Users (they expire, count their sign-ups, can be revoked and can
ask for an admin's approval; they never contain the invite code).

More admins: **Admin → Users**, or `docker compose exec slotserve slotserve set-admin <username>` (`--revoke` to
remove). Keep at least two admins so one can help the other.

## Reverse proxy {#reverse-proxy}

Put SlotServe behind a reverse proxy for HTTPS or a nicer address. Three things matter.

**1. The address: `Host` or `SLOTSERVE_PUBLIC_URL`.** SlotServe refuses changing requests (POST, PUT, PATCH,
DELETE) from a browser unless their `Origin` is SlotServe's own address: the host the request was sent to (`Host`,
or `X-Forwarded-Host`) or the origin of `SLOTSERVE_PUBLIC_URL`. A proxy that rewrites `Host` to `127.0.0.1:8000`
makes every change in the web UI fail with "did not come from a SlotServe page". Pass the original host, and set
`SLOTSERVE_PUBLIC_URL` to the address people type (it is also used in links SlotServe sends):

```bash
SLOTSERVE_PUBLIC_URL=https://gpu.example.org
SLOTSERVE_COOKIE_SECURE=true                 # cookies only over HTTPS
```

**2. Client addresses: `SLOTSERVE_FORWARDED_ALLOW_IPS`.** Sign-in throttling locks an account per address
(account + IP), the audit log records addresses and API keys show where they were last used. SlotServe only
believes `X-Forwarded-For` from the proxies listed in `SLOTSERVE_FORWARDED_ALLOW_IPS` (default `127.0.0.1`).
With Docker, the proxy's connections arrive from Docker's bridge address (for example `172.18.0.1`), not
`127.0.0.1`: sign in once, look at the address of your sign-in in **Admin → Audit log**, and if it is the proxy's
rather than your own, put that address in:

```bash
SLOTSERVE_FORWARDED_ALLOW_IPS=172.18.0.1     # comma separated; "*" only if port 8000 is reachable from the proxy alone
```

Without it, everybody shares the proxy's address and failed sign-ins from one person can slow down others.

**3. Streaming and the second port.** Turn off buffering so model answers stream, allow long requests, and pass
WebSockets. Dev environment web interfaces use port 8001 and need an address of their own (they must stay a
different origin from SlotServe): a second `server` block or a second host name, with `SLOTSERVE_DEVENV_WEB_URL`
set to it.

```nginx
server {
    listen 443 ssl;
    server_name gpu.example.org;
    # ssl_certificate ...;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 1h;
        client_max_body_size 0;
    }
}

server {
    listen 8443 ssl;                            # or another server_name on 443
    server_name gpu.example.org;
    # ssl_certificate ...;
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

```bash
SLOTSERVE_DEVENV_WEB_URL=https://gpu.example.org:8443
```

The OpenAI-compatible API (`/v1`) works through the same proxy; clients use `https://gpu.example.org/v1`. SSH and
direct ports of dev environments do not go through the proxy (see [Ports and firewall](#firewall)).

## Health checks and `slotserve doctor` {#health}

One set of checks feeds every view of SlotServe's health:

| Where | What | Who |
| --- | --- | --- |
| **Admin → Overview** | Setup checklist, then the **System health** table: every check with its evidence, buttons, a diagnosis for recognised errors, and dismissing with a reason | Admins |
| `GET /healthz` | Shallow: the process answers (Docker's `HEALTHCHECK` uses it) | Anyone |
| `GET /api/health` | Every check as JSON (`checks[]`, `status: ok / degraded / down`); `?strict=true` answers 503 while a check fails, for uptime monitors | Admin session or the metrics token (`Authorization: Bearer <SLOTSERVE_METRICS_TOKEN>`) |
| `GET /metrics` | Prometheus: `slotserve_check_status`, `slotserve_backup_age_seconds` and the GPU and usage metrics | Metrics token |
| `slotserve doctor` | The same checks from the command line | Shell on the server |

```bash
docker compose exec slotserve slotserve doctor          # while the server runs
docker compose run --rm --no-deps slotserve slotserve doctor   # when it does not start
slotserve doctor --json                                 # machine-readable, secrets removed
```

`doctor` prints one line per check (`ok`, `info`, `WARN`, `ERROR`) with the evidence of warnings and errors, and
exits with code 1 when a check is at level error. It runs in its own process, so checks of the running server's
internals (lanes, background tasks) are only in the web UI and `/api/health`.

| Check | What it looks at | When it is not OK |
| --- | --- | --- |
| database | The database can be written | Disk full or read-only, file permissions |
| disk | Free space where data and models live, and on storages | [Disk full](#disk-full) |
| engine | The inference engine program exists | Fix `LLAMA_CPP_DIR` / `SLOTSERVE_LLAMA_SERVER_CMD`; see [Configuration](configuration.md#llama-server) |
| lanes | Every lane's model server | A failed load, with its diagnosis; [FAQ: diagnoses](faq.md#diagnosis) |
| models | At least one model, an idle default per lane | Add a model, set a default (Admin → GPUs & lanes) |
| gpu | `nvidia-smi` sees GPUs | [GPUs gone](#gpu-gone) |
| docker | Docker is reachable (dev environments on) | [Dev environments: Docker](dev-environments.md#docker) |
| storage | Every storage mounts and is writable | [Storage](storage.md#types), the diagnosis on the check |
| chat | Chat platforms are connected | Admin → Chat channels |
| backup | The last backup and its copy off the machine | [Backups](#backups) |
| backup_age | The newest backup is younger than 36 hours | [When a backup fails](#backup-alerts) |
| restore | A restore waits for its reconciliation | [The reconciliation report](#reconcile) |
| migration | The last upgrade kept every row | `data/migration_report.json` |
| tasks | Background tasks run and report back | Restart SlotServe; report the evidence |
| secrets | Stored secrets can be decrypted | [Secret key lost](#secret-key-lost) |
| security | Number of admins; dev environments mean root on the host | A note to read once |
| invite | The invite code is not `change-me` | Set an invite code or use invite links |

Some results are notes rather than problems (no chat platform on purpose, a machine without GPU): **Dismiss…**
them with a reason. A dismissed check stays in the table, no longer counts as a problem, and the reason is in the
audit log; it comes back on its own when its level changes.

## Inbox and alerts {#alerts}

Every notification goes into the in-app **inbox** first (the bell in the header) and then to the chat platforms
the person linked. Members get the news about their own work (approvals, reminders, environments that are ready or
failed). Admins also get the admin messages: approvals to give, sign-ups waiting, failed model loads, storages
that stop working, sensitive settings changes, and the health alerts.

- A check that reaches its alert level (an error, for some checks a warning, some only after a while) sends **one**
  alert to all admins, and one more when it recovers. There are no repeats, also not across restarts. Dismissed
  checks do not alert.
- Group webhooks (Feishu / Lark, DingTalk, WeCom) can get admin alerts too: **Admin → Chat channels**.
- Without any chat platform nothing is lost: the inbox has everything. The Admin menu item shows how many admin
  messages are unread.
- For monitoring from outside, poll `/api/health?strict=true` or scrape `/metrics` (`slotserve_check_status`).

## Container hardening and compatibility mode {#hardening}

Every container SlotServe starts for a dev environment is hardened in one fixed last step:

- **no new privileges**: setuid programs such as `sudo` cannot gain rights;
- capabilities `NET_RAW`, `MKNOD`, `SYS_CHROOT`, `AUDIT_WRITE` and `SETFCAP` are dropped (`ping` does not work);
- at most `SLOTSERVE_DEVENV_PIDS_LIMIT` processes (default 4096), so a fork bomb stays inside its container;
- a memory limit (the **Memory limit** setting, or with 0 the host's memory less max(4 GB, 10 %)), so an
  out-of-memory job is killed inside its container instead of taking SlotServe down;
- bounded logs, exactly the lane's GPUs, never privileged, never the host's network or process namespace, never
  the Docker socket.

People are `root` inside their container, so `apt install` works without `sudo`. What changes:

| Symptom inside an environment | Why | What to do |
| --- | --- | --- |
| `sudo: effective uid is not 0` or a setuid program fails | No new privileges | Run as root without `sudo`. For an image that must use setuid (its own init system, a user switch), an admin turns on **Compatibility mode** in that template's settings (Dev environments, admins only; the built-in desktop already uses it). Less isolated: only for images you trust. |
| `ping: permission denied` | `NET_RAW` is dropped | Use `curl` or `nc` to test connections. |
| `fork: Resource temporarily unavailable` | The process limit | Fewer parallel workers, or raise `SLOTSERVE_DEVENV_PIDS_LIMIT`. |
| A job is `Killed` | The memory limit | A smaller batch, or a higher **Memory limit**. Admins see the limit that applies in `/api/devenv` (`hardening`). |

**Web interface compatibility mode** (Admin → System settings, `SLOTSERVE_DEVENV_WEB_COMPAT`) is a different
switch: it relaxes the rules that keep the pages of one environment from acting in another one opened in the same
browser (on the web port 8001). Turn it on only when an app in an environment's web interface does not work and
nothing else helps; it weakens the isolation between environments and nothing else.

## Ports and firewall {#firewall}

| Port | What | Who needs to reach it |
| --- | --- | --- |
| **8000** (`PORT`) | Web UI, `/api`, the OpenAI-compatible `/v1`, `/metrics` | Everyone (or only the reverse proxy) |
| **8001** (`DEVENV_WEB_PORT`, `SLOTSERVE_DEVENV_WEB_PORT`) | Dev environment web interfaces (VS Code, JupyterLab, desktop, your own web apps) | Everyone who uses them (or only the reverse proxy) |
| **8002** (`SLOTSERVE_DEVENV_GATEWAY_PORT`) | The container-side gateway: only `/v1`, for code inside dev environments | Only the dev environment containers; never open it to the network |
| **2200-2299** (SSH ports) | SSH into dev environments, one port per running environment | Everyone who uses SSH |
| **2300-2399** (Direct port range) | Ports people publish directly from their environments | Everyone who uses them; a direct port has no SlotServe login |

- The ranges are settings (**SSH ports**, **Direct port range**); keep them apart from each other and from ports
  the host uses.
- **Docker publishes the SSH and direct ports itself**, through its own iptables rules, which bypass `ufw` and
  `firewalld` rules on the host. To restrict who can reach them, use the `DOCKER-USER` chain or a firewall in front
  of the machine, for example:

    ```bash
    # only the 10.0.0.0/8 network may reach the SSH and direct ports of dev environments
    iptables -I DOCKER-USER -p tcp -m multiport --dports 2200:2399 ! -s 10.0.0.0/8 -j DROP
    ```

- **Port 8002** needs nothing when SlotServe runs in Docker: the gateway lives inside SlotServe's container and is
  reached as `slotserve-gw` on each environment's network. When SlotServe runs directly on the host, it listens on
  Docker's bridge address (`docker0`); a host firewall that drops traffic from containers to the host must allow
  TCP 8002 from Docker's networks (by default inside `172.16.0.0/12` and `192.168.0.0/16`), for example
  `ufw allow from 172.16.0.0/12 to any port 8002 proto tcp`.
- Behind a reverse proxy, 8000 and 8001 can be bound to `127.0.0.1` (`ports: - "127.0.0.1:8000:8000"`) so that
  only the proxy reaches them.

## Runbooks {#runbooks}

### Forgotten admin password or locked out {#admin-password}

**Symptom.** An admin cannot sign in; or "too many attempts".

**Diagnose.** Failed sign-ins lock an account for 15 minutes from the address they came from (10 failures per
account and address, 30 per address); other addresses are not affected. Behind a proxy without
`SLOTSERVE_FORWARDED_ALLOW_IPS`, everyone shares one address ([Reverse proxy](#reverse-proxy)).

**Fix.**

- Another admin resets the password in **Admin → Users**.
- On the server:

    ```bash
    docker compose exec slotserve slotserve reset-password <username>    # asks for the new password
    docker compose exec slotserve slotserve set-admin <username>         # if no admin is left
    ```

    The reset signs that account out everywhere. The lock is held in memory: `docker compose restart slotserve`
    clears it.
- No admin account at all any more: [setup code](#setup-code).

**Prevent.** Two admins; passwords in a password manager.

### Disk full {#disk-full}

**Symptom.** The health check **disk** warns or errors; backups, downloads or model loads fail with "No space left
on device"; the database cannot be written.

**Diagnose.**

```bash
df -h                                  # which file system
du -sh data/* | sort -h | tail         # SlotServe's data directory
docker system df                       # images, containers, volumes, build cache
du -sh "$MODELS_DIR"
```

Usual culprits: models, Docker images (environment templates, saved images, kept environments' snapshots),
`/workspace` volumes, old backups and upgrade snapshots in `data/backups/`, logs.

**Fix.**

- Models no one uses: delete them in **Models** (removes the file).
- Docker: `docker image prune` (unused images), and saved environment images people no longer need (deleting the
  saved template removes its image). Do not `docker volume prune` blindly: volumes hold people's `/workspace`.
- Backups: upgrade snapshots and `prerestore` bundles are never removed automatically; delete old ones by hand
  once you no longer need them. Lower `backup_keep` if needed.
- Logs: model server logs rotate at 20 MB, the SlotServe container's Docker log at 5 × 50 MB. Containers of dev
  environments are limited the same way.
- Usage records are folded after `SLOTSERVE_USAGE_RETENTION_DAYS`; the audit log is kept for
  `SLOTSERVE_AUDIT_RETENTION_DAYS`.

**Prevent.** Models and Docker's data root on a different disk than the system; a backup target; set a soft limit
on personal storages; watch the disk check's warning (it starts at twice the download free-space minimum, at least
5 GB).

### Dev environment does not start {#devenv-start}

**Symptom.** An environment's card says it did not start; the person was notified.

**Diagnose.** As admin, **Dev environments → Everyone**, open the environment: its error and **Logs**. Recognised
errors come with a diagnosis (cause and next steps); see [FAQ: diagnoses](faq.md#diagnosis). Then by cause:

| Cause | Typical error | Fix |
| --- | --- | --- |
| Image pull | `pull access denied`, `manifest unknown`, `toomanyrequests` | Check the image name and tag; the Docker host's access to the registry (proxy, mirror); `docker login` on the host for private images or Docker Hub limits; pull it once by hand. |
| Port conflict | `port is already allocated`, `address already in use` | Something else on the host uses a port of the SSH or direct range: move the range (settings) or the other program. |
| Mounts | `failed to mount local volume`, `mount error(13)`, `access denied by server` | Storage: **Check** it in Admin → Storage; install `nfs-common` / `cifs-utils` on the Docker host; NFS exports must allow the host. Storage directory: must be a path on the Docker host. |
| CPU set | `Requested CPUs are not available`, `cpuset` | Fewer cores than lanes reserve: lower **CPU cores reserved per GPU**; after a CPU change restart SlotServe so it reads the host again. |
| GPU | `could not select device driver "nvidia"` | [GPUs gone](#gpu-gone) |
| Lane without GPUs | `devenv_lane_no_gpus` | Give the lane its GPUs in Admin → GPUs & lanes. |
| Docker | `Cannot connect to the Docker daemon` | Docker running? Socket mounted? See [Dev environments: Docker](dev-environments.md#docker). |

**Fix.** Fix the cause, then **Try again** on the environment.

**Prevent.** Run `python -m tools.verify` after changes to Docker, drivers or templates ([Verify](#verify));
prefer images from registries you control for important templates.

### GPUs gone after a driver or Docker upgrade {#gpu-gone}

**Symptom.** The health check **gpu** says "No GPU detected"; models fail with CUDA errors; dev environments fail
with `could not select device driver "nvidia"`.

**Diagnose.**

```bash
nvidia-smi                                                  # on the host: does the driver work?
docker run --rm --gpus all ubuntu nvidia-smi                # can Docker give containers a GPU?
docker compose exec slotserve nvidia-smi                    # does SlotServe's container see them?
```

**Fix.**

- `nvidia-smi` fails on the host: the kernel module does not match the new driver; reboot, or reinstall the driver
  for the running kernel.
- Only Docker fails: reconfigure the NVIDIA Container Toolkit and restart Docker:

    ```bash
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    ```

- Models fail with "CUDA driver version is insufficient": the engine needs a newer driver, or rebuild it for the
  installed CUDA; with Docker the CUDA major of `CUDA_IMAGE` must match your `llama-server` build.
- Then `docker compose up -d` (a Docker restart stops SlotServe unless `restart: unless-stopped` brings it back).

**Prevent.** Pin the driver packages (`apt-mark hold`), upgrade drivers and Docker in a maintenance window, and run
`slotserve doctor` and `tools/verify` afterwards.

### Suspected account or key leak {#leak}

**Symptom.** Unknown sign-ins, API usage nobody recognises, a key or password posted somewhere.

**Diagnose.**

- **Admin → Audit log**: filter by the person, by action (`auth.*` for sign-ins and failed sign-ins, `key.*` for
  API keys) and dates; addresses are recorded (see [Reverse proxy](#reverse-proxy) for real addresses). Export the
  selection as CSV for the record.
- **Admin → Request log**: requests by person, key and source (API key, environment key, Playground); each API key
  shows the address it was last used from.

**Fix.**

1. **Disable the account** (Admin → Users) if it is compromised: it is signed out everywhere and its bookings end.
2. **Revoke API keys** (Admin → API keys, or the person under **Access credentials**). Environment keys end with
   their environment.
3. **Sign out everywhere**: the person uses **Settings → Signed-in devices → Sign out all other devices**; a
   password change or an admin's reset does the same.
4. Remove SSH keys the person does not recognise (Access credentials) and set a new environment password.
5. If an admin account or the server's `.env` leaked, also [rotate the secret key](#secret-key-lost) and change
   the invite code, chat platform tokens and `SLOTSERVE_METRICS_TOKEN`.

**Prevent.** Keys with an expiry, invite links instead of a shared invite code, few admins, and review the audit
log now and then.

## Verify on real hardware {#verify}

`tools/verify` checks a running installation end to end, through its public API, the way people use it: a GPU
booking and an OpenAI-style request, every built-in dev environment template with SSH (key and password) and its
web interface, an environment without GPU, a storage, the container hardening and the security protections. It
creates a throw-away account and removes it again; it changes no settings (a feature that is off is reported as
skipped). Run it after installing, after upgrades of SlotServe, drivers or Docker, and for every release candidate.

From a checkout of the SlotServe repository on a machine that reaches the server (Python 3.11+ with `httpx`, an
OpenSSH client; `sshpass` for the password logins):

```bash
python -m tools.verify --url http://gpu-server:8000 --admin <admin account> --out reports/
```

The admin password is asked for (or `SLOTSERVE_VERIFY_PASSWORD`). Useful options: `--lane`, `--model`,
`--templates pytorch,vscode` (a subset), `--storage-id` or `--storage-host-path`, `--no-ssh`, `--no-web`,
`--save-image`, `--destructive` (also a fork bomb in an environment without GPU), `--keep` (keep the test account
for debugging), `--insecure` (self-signed TLS). Pulling every template image takes a while the first time.

It writes `verify-report-<host>-<version>.md` and `.json`. Results are **pass**, **fail**, **skip** (a feature is
off or a tool is missing), **pending** or **manual**: the manual ones are browser checks the tool cannot do (a
CSRF proof-of-concept page written next to the report, and the isolation of two environments' web pages), with
instructions in the report. Exit code 0 means nothing failed, 1 that a check failed, 2 that the server could not be
reached or signed in to. Attach the report to the release pull request (see the release checklist).

# Personal computers

A small lab or an engineering dorm often has a few shared machines plus everyone's own computer — gaming laptops
and desktops with a GPU, frequently on Windows. SlotServe can pool them: each personal computer joins as a
[node](multiple-machines.md) with an **owner** who decides when it is shared, can take it back at any moment,
and sees what others run on it. The control plane can be anything that is always on: a NAS, an old PC or a
Raspberry Pi.

Nodes only ever connect **out** to the server, so a personal computer needs no open port.

## Add a personal computer

1. An admin opens **Admin → Nodes → Add node**, names the machine (e.g. `alice-desktop`) and copies the
   **one-line install** command shown with the token. The token is shown only once; **New token** issues another.
2. The admin sets the machine's **Owner** (Admin → Nodes → Edit). The owner then finds it under
   **My machines**.
3. On the computer, run the install line for its system (below). The machine shows as **Online** within seconds;
   Admin → Nodes also shows what kind of machine it is (e.g. *Windows · WSL2 (Docker Desktop)*).
4. In **Admin → GPUs & lanes**, add a lane on the machine and pick its GPUs.

The install scripts are served by your SlotServe server at `/install/node.sh` and `/install/node.ps1` with the
server address and the token filled in. Running the same line again replaces the agent (for example after a new
token). Both put the models folder and llama.cpp under `~/slotserve` (Windows: `%USERPROFILE%\slotserve`) unless
`SLOTSERVE_MODELS_DIR` / `SLOTSERVE_LLAMA_CPP_DIR` say otherwise.

!!! warning "llama.cpp must be a Linux build"
    The agent runs in a Linux container on every system. Put a **Linux** build of llama.cpp (`llama-server` and its
    libraries; the CUDA build for NVIDIA GPUs) into the llama.cpp folder — also on Windows, where the Windows `.exe`
    does not work. Without it the machine joins but cannot run models. vLLM / SGLang need the corresponding images
    or commands as on any node.

=== "Linux"

    ```bash
    curl -fsSL 'https://slotserve.example/install/node.sh?token=ssn-...' | bash
    ```

    The script checks that Docker runs and, when the machine has an NVIDIA GPU, that the
    [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
    is installed, then starts the `slotserve-agent` container with `--restart unless-stopped`, `--gpus all` and host
    networking (so the agent sees the real network card for Wake-on-LAN and can wake its neighbours). Set
    `SLOTSERVE_NO_GPU=1` for a CPU-only node.

=== "Windows"

    Requirements: Windows 10 22H2 or 11, a current NVIDIA driver (for GPUs) and
    [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the **WSL 2 based
    engine** and **Linux containers**. In PowerShell:

    ```powershell
    irm 'https://slotserve.example/install/node.ps1?token=ssn-...' | iex
    ```

    The script checks Docker Desktop, the WSL 2 engine and that containers can use the GPU
    (`docker run --gpus all ... nvidia-smi`), finds the active physical network card (its MAC address and subnet,
    for Wake-on-LAN, and whether *Wake on Magic Packet* is enabled) and starts the agent with a restart policy.

    - Turn on **Start Docker Desktop when you sign in** (Docker Desktop → Settings → General), so the agent comes back
      after a restart. Docker Desktop starts after sign-in, so a PC woken over the network should sign in
      automatically, or the machine stays offline until someone logs in.
    - Models in a Windows folder are read through WSL's file sharing, which is slower than a Linux disk; large
      models load more slowly. Pointing `SLOTSERVE_MODELS_DIR` at a folder inside the WSL distribution
      (`\\wsl$\...`) is faster.
    - Windows *Fast Startup* can keep the network card from waking a PC that was shut down; switch it off
      (Control Panel → Power Options → Choose what the power buttons do) if Wake-on-LAN does not work from
      shutdown.

=== "macOS"

    ```bash
    curl -fsSL 'https://slotserve.example/install/node.sh?token=ssn-...' | bash
    ```

    Docker Desktop on a Mac cannot pass the Apple GPU into containers, so a Mac joins as a **CPU-only node**: its
    lanes have no GPUs and run CPU builds of llama.cpp (a Linux arm64 build on Apple silicon). Running the agent
    natively instead (`pip install` SlotServe, then `slotserve agent --server ... --token ...`) lets a Metal build
    of llama.cpp use the GPU, but SlotServe still shows the lane without GPUs.

Without the scripts, the `docker run` command shown with the token and `docker-compose.node.yml` still work
(see [Multiple machines](multiple-machines.md#add-a-machine)).

## Owner controls

The owner (and admins) manage a machine on **My machines**, or from **Admin → Nodes → Sharing**.

### Sharing hours

**Always shared** (the default), **On a schedule** or **Never** (only the owner uses it). A schedule is a list of
weekly windows in the server's time zone (`timezone` in Admin → System settings); a window that ends before it
starts runs into the next day. **Nights and weekends** fills in weekdays 23:00–09:00 and the whole weekend.

- **Bookings** by others must lie entirely inside the shared hours; otherwise they are refused with a clear message
  (also when extending a booking, and for the waitlist). The owner can book their own machine at any time. The
  booking calendar hatches the hours a lane's machine is not shared, and the booking dialog warns before you
  submit.
- Outside the shared hours the machine's lanes run **nothing on their own**: no default model and no shared dev
  environment. Bookings that already exist still run (they were made when the machine was shared).
- When a new schedule leaves existing bookings of others outside the shared hours, they **stay booked**, but the
  people affected are told to move them, and the owner gets a summary (also shown right after saving).

### "I'm using my PC"

**Pause sharing** for 1–8 hours or until resumed:

1. Everyone with a booking on the machine that runs now or starts during the pause is told at once.
2. During a grace period (`sharing_grace_minutes`, 10 by default) everything keeps running, so people can save
   their work.
3. After the grace period, running bookings of others are ended. Their kept dev environments are **paused, not
   deleted**, exactly like at the end of any booking. The lanes then run nothing (except the owner's own bookings)
   until the pause ends; nobody else can book the machine for the paused time.

**Resume sharing** ends the pause at once.

### What runs on my machine

A list of the machine's lanes (which model runs and why), bookings that run now or start in the next 24 hours, and
dev environments. **Stop** ends a running booking (or cancels a coming one) and tells the person; **Stop until the
next booking** unloads a lane's default model or shared environment.

## Wake-on-LAN

A machine that sleeps or is switched off can be woken shortly before a booking on it starts
(`wol_lead_minutes`, 5 by default; 0 switches it off) and with the **Wake now** button (for the owner, admins and
people whose booking on it starts within the hour). Because the control plane may be in another network, the magic
packet is sent by an **online machine in the same local network**: another node agent, or the server itself.
Machines count as one network when they share a **network label** (set on the machine, or `SLOTSERVE_AGENT_NETWORK`;
the Windows script uses the PC's subnet, e.g. `192.168.1.0/24`) or the subnet of a physical network card.

The target's **MAC address** is reported by the agent when it sees the real network card (Linux with host
networking), passed by the install scripts (Windows, macOS, `SLOTSERVE_AGENT_WOL_MAC`), or entered by hand. If the
machine is still offline when the booking starts, the booker and the owner are notified.

Settings on the machine to be woken:

- **BIOS / UEFI**: enable *Wake on LAN* / *Power On by PCI-E* / *Resume by LAN* (names vary), and disable *ErP / EuP*
  or *Deep Sleep* modes that cut power to the network card.
- **Windows**: Device Manager → network adapter → *Power Management*: allow the device to wake the computer, *only
  allow a magic packet*; *Advanced*: *Wake on Magic Packet* enabled. Disable Fast Startup to wake from shutdown.
- **Linux**: `ethtool -s <nic> wol g` (make it permanent with your network manager, e.g.
  `nmcli connection modify <con> 802-3-ethernet.wake-on-lan magic`).
- **macOS**: System Settings → Energy → *Wake for network access*.
- Use a **wired** connection; Wi-Fi cards rarely wake a sleeping computer. Magic packets are broadcasts and do not
  cross routers: the sending machine must be in the same network segment.

## Contribution

Owned machines that are online are measured once a minute: hours **online**, hours **shared** (online, within the
sharing hours and not paused) and **GPU-hours offered** (shared hours × GPUs of the machine's lanes). Together with
the usage samples this gives, per person: what their machines offered, how many GPU-hours **others used** on them,
and how many GPU-hours they **used themselves** (bookings on any machine). The **Usage** page shows the table (CSV
export) and each machine's numbers are on its sharing panel. API: `GET /api/usage/contribution`,
`GET /api/usage/contribution/export`, `GET /api/nodes/{id}/contribution`.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `sharing_grace_minutes` (`SLOTSERVE_SHARING_GRACE_MINUTES`) | `10` | Time between "pause sharing" and stopping others' work |
| `wol_lead_minutes` (`SLOTSERVE_WOL_LEAD_MINUTES`) | `5` | Wake an offline machine this long before a booking on it; 0 = never automatically |
| `SLOTSERVE_WOL_PORT` | `9` | UDP port of the magic packet |

Agent: `SLOTSERVE_AGENT_WOL_MAC` (MAC addresses, comma separated), `SLOTSERVE_AGENT_NETWORK` (network label),
`SLOTSERVE_AGENT_HOST_OS` (`windows` / `macos` / `linux`, set by the install scripts).

## API

| Method and path | Who | What |
| --- | --- | --- |
| `GET /api/nodes/mine` | everyone | Machines you own |
| `GET`, `PUT /api/nodes/{id}/sharing` | owner, admin | Sharing hours (`schedule`: `null` always, `[]` never, or windows), `network`, `mac_addresses`; `PUT` returns `conflicts` |
| `POST /api/nodes/{id}/pause` `{hours}` / `POST .../resume` | owner, admin | Pause (hours `null` = until resumed) / resume |
| `GET /api/nodes/{id}/running`, `POST .../stop` `{reservation_id}` or `{lane_id}` | owner, admin | What runs; stop it |
| `POST /api/nodes/{id}/wake` | owner, admin, booker | Send the magic packet through a machine in the same network |
| `GET /api/lanes/unavailable?start=&end=` | everyone | Per lane, when its machine is not shared |

`GET /api/admin/nodes` includes `owner_id`, `owner_name`, `sharing_schedule`, `shared_now`, `sharing_paused`,
`sharing_paused_until`, `sharing_next_change`, `platform`, `host_os`, `network`, `wol_macs` and `woken_at`.

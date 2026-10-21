# Dev environments

Besides running a model or taking the whole GPU, a booking can be a **dev environment**: for the length of the booking SlotServe runs a Docker container with the lane's GPUs and an SSH server, and you log in with your SSH key or your environment password. Your files in `/workspace` are kept between bookings, and you can keep the whole environment too. Lanes can also run a **shared environment** everyone can log into while nobody has them booked, and anyone can run an environment [without a GPU](#without-a-gpu) on the shared CPU cores at any time.

```mermaid
sequenceDiagram
    participant U as You
    participant S as SlotServe
    participant D as Docker
    U->>S: book a dev environment (template, time)
    Note over S,D: up to an hour before the start
    S->>D: pull the image
    Note over S,D: at the start
    S->>S: stop the lane's model server
    S->>D: create a network and the container (lane GPUs, /workspace, /models read-only)
    S->>D: copy in the SSH server and your public keys, start
    S-->>U: ssh -p 2201 root@server (web page and Telegram)
    U->>D: ssh, VS Code Remote-SSH, port forwarding
    Note over S,D: when the booking ends
    S->>D: stop and remove the container (/workspace stays; a kept environment is only stopped)
    S->>S: load the lane's default model again
```

## Setting it up (admin)

1. Mount the Docker socket into SlotServe. In `docker-compose.yml` uncomment

    ```yaml
    - /var/run/docker.sock:/var/run/docker.sock
    ```

    and run `docker compose up -d`. Containers need GPU access through the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), which the SlotServe container already uses.

2. Turn on **Admin → System settings → Dev environments → Dev environments**.

3. Open the **SSH ports** range (default `2200-2299`) in the host firewall. Each running environment gets one port from it.

4. Optionally set **SSH host name** (what people should type after `root@`), CPU, memory and shared-memory limits, which image registries are allowed, whether **Password logins** are allowed, a **Storage directory** for persistent data, and the limits for kept environments.

!!! warning "Security"
    Whoever controls the Docker socket controls the host. SlotServe only starts containers without extra privileges, with the lane's GPUs, the person's `/workspace` volume and the models directory read-only, but anyone with admin rights in SlotServe can now run containers on the host. Only enable dev environments on a server whose users you trust.

    **Podman** provides a Docker-compatible API without a root daemon. Point `SLOTSERVE_DOCKER_HOST` at its socket (for example `unix:///run/podman/podman.sock`) to use it instead; GPU access then needs Podman's CDI setup.

## Using it

1. On **Access credentials**, add an SSH public key or set an environment password (see below).
2. Press **Book an environment** (or choose **Dev environment** in the booking dialog), pick a template (or a kept environment) and a time.
3. The image is downloaded ahead of time; the page shows the progress. When the booking starts, the page shows the command to connect:

    ```bash
    ssh -p 2201 root@server
    ```

    You are `root` inside the container. Your files belong in `/workspace`; the shared models are at `/models` (read-only).

4. **VS Code**: add the snippet from the page to `~/.ssh/config` and connect with Remote-SSH. The port can change between bookings.
5. **Desktop, JupyterLab, VS Code in the browser**: templates with a web interface show an **Open desktop / Open JupyterLab / Open VS Code** button while they run (see [Web interfaces](#web-interfaces)). Other web tools you start yourself can be reached with port forwarding, e.g. `ssh -p 2201 -L 8888:localhost:8888 root@server`.

When the booking ends the container is removed, unless it is a kept environment. Extend the booking in time (the reminder 10 minutes before the end has buttons for that) if you need longer.

### Templates

| Template | Image | Web interface |
| --- | --- | --- |
| PyTorch | `pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel` | |
| JupyterLab + PyTorch | `quay.io/jupyter/pytorch-notebook:cuda12-pytorch-2.11.0` | JupyterLab |
| TensorFlow | `tensorflow/tensorflow:2.21.0-gpu-jupyter` | |
| CUDA | `nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04` | |
| Ubuntu desktop | `linuxserver/webtop:ubuntu-xfce-388f14a2-ls7` | Desktop (XFCE) |
| VS Code | `codercom/code-server:4.141.0` | VS Code (code-server) |

The built-in templates use CUDA 12 builds, which run on a wide range of GPUs and drivers. Admins can change their images, switch them off or add shared templates.

Anyone can add **their own template** from any image (unless an admin turned custom images off or limited the registries), with an optional main process and environment variables. The image needs `/bin/sh`; SSH works in any image because SlotServe brings its own small static SSH server.

### Web interfaces

SlotServe opens any port of a running environment in the browser at `/devenv/<session>/port/<port>/` on the SlotServe address. A template **declares its web interfaces** as a list of ports, each with a name and a kind (desktop, JupyterLab, VS Code or any other web app); while the environment runs, its card has an **Open …** button for each of them (new tab). Other ports, e.g. a TensorBoard you start yourself on port 6006, can be opened by typing `/devenv/<session>/port/6006/`. Only the person who booked the environment and admins can open its ports (environments that belong to no booking are open to everyone signed in); you need to be signed in to SlotServe.

```mermaid
flowchart LR
    B[Browser] -- "/devenv/7/port/8888/ (session cookie)" --> S[SlotServe]
    S -- "HTTP + WebSocket, token added" --> C["container slotserve-dev-7:8888"]
    subgraph N["network slotserve-devenv-7"]
        C
    end
```

- **Isolation**: every environment runs on its own Docker network. Web ports are never published: when SlotServe runs in a container it joins the environment's network and reaches any port by container name; when it runs directly on the host, Docker binds each declared port to a random port on `127.0.0.1`, and other ports are reached on the container's address in its network (on Linux hosts). The network is removed when the environment stops (a kept environment keeps its network, and its web addresses, until it is deleted), and leftovers are cleaned up after a restart. A lane's shared environment gets a network too; its web interfaces are open to everyone signed in.
- **Proxy**: SlotServe forwards HTTP (streamed) and WebSockets (Jupyter kernels, VS Code, desktop streaming), keeps redirects and cookies under the environment's path and never passes its own session cookie on.
- **Token**: each start gets a random token. JupyterLab is started with it (`--IdentityProvider.token`) and the desktop uses it as its password; SlotServe adds it to every request, so nobody can use these apps without going through SlotServe.
- **Base path**: the main process and environment variables can use the placeholders `{base_path}` and `{port}` (the first declared port, e.g. `/devenv/7/port/8888/` and `8888`), `{base_path_<port>}` (any declared port, e.g. `{base_path_6006}`), `{token}` and `{session}`. Ports whose app is given its base path (Jupyter `--ServerApp.base_url`, the desktop's `SUBFOLDER`) receive the full path; for all other ports (code-server, most simple apps, ports that are not declared) SlotServe strips the prefix, so those apps should use relative links. The variables `SLOTSERVE_WEB_BASE`, `SLOTSERVE_WEB_PORT` and `SLOTSERVE_WEB_TOKEN` (first declared port) are set as well.
- **Keep the image's entrypoint**: images such as the linuxserver desktop run their own init system. With this option SlotServe keeps the image's entrypoint and command and starts the SSH server next to it with `docker exec`, instead of using its own start script.

The built-ins: **JupyterLab + PyTorch** (JupyterLab on port 8888, files in `/workspace`), **Ubuntu desktop** (an XFCE desktop streamed to the browser, port 3000) and **VS Code** (code-server on port 8080, opened on `/workspace`; extensions come from Open VSX). SSH works in all of them as before.

!!! note
    The desktop streams video with modern browser APIs that need a secure context: open SlotServe over HTTPS (or on `localhost`). Hardware-accelerated graphics in the desktop may need `NVIDIA_DRIVER_CAPABILITIES=all` in the template's environment variables, depending on the host's driver setup.

!!! warning "Same origin"
    Web interfaces are served from the same address as SlotServe. A page in someone's environment runs with the rights of whoever opens it, so admins should only open environments of people they trust. code-server has no token of its own; it relies on the per-environment network (and the port bound to `127.0.0.1` when SlotServe runs on the host).

### Saving an environment

**Save as image** stores the running container (installed packages, files outside `/workspace`) as a new template of yours, so the next booking starts exactly where you left off. `/workspace` is kept anyway and is not part of the image. The number of saved images per person is limited (default 3); deleting a saved template removes the image from the server.

## Logging in: SSH keys and the environment password

Your credentials are on the **Access credentials** page (next to your API keys):

- **SSH public keys**: paste the content of `~/.ssh/id_ed25519.pub` (create one with `ssh-keygen -t ed25519`).
- **Environment password**: an SSH password for dev environments, separate from your SlotServe password. SlotServe stores only its SHA-512-crypt hash (the `/etc/shadow` format) and writes that hash into the container; the password itself is never stored. Each connection gets three tries.

A dev environment booking needs an SSH key, or an environment password while **Password logins** is on (the default). Admins can switch password logins off; then only keys work and SSH servers reject passwords. A new password reaches a running booked environment the next time it starts; shared environments pick it up within a minute.

## Kept environments (projects)

Normally the container is removed when the booking ends and only `/workspace` stays. To keep everything you installed, choose **Keep as new** in the booking dialog and give the environment a name. When that booking ends the container is only stopped (paused). A later booking can **Continue** it: the same container starts again, with your current keys and password copied in.

- If the next booking is on a lane with different GPUs, SlotServe saves the container as an image (a snapshot) and recreates it on the new GPUs, keeping its kept directories and settings. The same happens when its kept directories changed or its old SSH port is taken.
- **Kept directories**: besides `/workspace`, a template or kept environment can list more directories to keep, e.g. `/root` or `/opt/conda` (absolute paths; system directories such as `/etc`, `/usr`, `/proc` or `/opt/slotserve` are not allowed). A new directory starts with the image's content. Changing the list takes effect the next time the container is created.
- The **Dev environments** page lists your kept environments with their state (paused, running, not started yet, container missing), size and last use. You can rename them, change their kept directories and delete them; deleting removes the container and its snapshot, and optionally its kept directories. `/workspace` is never deleted.
- One kept environment cannot be booked twice at the same time. Admins limit how many each person may keep (**Kept environments per person**, default 3) and can remove kept environments unused for a number of days (**Remove unused kept environments after**; 0 = never; their data stays).

## Without a GPU

Not everything needs a GPU. Editing code, moving data, installing packages or a CPU job can run in a dev environment **without a GPU**, at any time and without a booking:

- On the **Dev environments** page, press **Start without GPU** in the **Without a GPU** section (or on a template or kept environment) and pick a template or one of your kept environments. It starts right away on the server's shared CPU cores, with the same SSH login, `/workspace`, kept directories and web interfaces as a booked environment, but no GPU.
- It runs until you **Pause** it (a kept environment keeps its container) or **Stop** it (any other environment; `/workspace` stays). There is no idle timeout and no end time. Its card shows the CPU cores and memory it uses right now.
- Admins limit how many each person may run at once (**Without-GPU environments per person**, default 2).

**One kept environment, two modes.** A kept environment can run without a GPU between its bookings. When a booking of it starts, SlotServe stops it and continues it with the booked lane's GPUs: the container is saved as a snapshot and recreated with the GPUs, so everything installed stays (as when a kept environment moves to other GPUs). When the booking ends it is paused as usual; with **Continue without GPU after bookings** switched on for that kept environment it goes on running without a GPU instead. You can also start it without a GPU again at any time. While a booking of it runs, it cannot be started without a GPU.

!!! note "Snapshots"
    Each switch between the CPU and a booking's GPUs saves the container as an image layer. If a kept environment switches very often over a long time, delete it and keep a fresh one now and then (or save it as an image and start a kept environment from that), because images have a limit on the number of layers.

### Sharing the CPU fairly

```mermaid
flowchart LR
    subgraph Host CPU cores
        A["Lane A: cores per GPU x its GPUs"]
        B["Lane B: cores per GPU x its GPUs"]
        P["Shared pool: all other cores"]
    end
    A --- BA["Booking / shared environment on lane A"]
    B --- BB["Booking / shared environment on lane B"]
    P --- C1["Environment without GPU"]
    P --- C2["Environment without GPU"]
```

- Every GPU lane **reserves CPU cores** for whoever uses it: **CPU cores reserved per GPU** (default 8) times its number of GPUs (a lane that does not restrict its GPUs counts as one). Its booking containers and its shared idle environment run on exactly those cores (Docker cpuset), so CPU-only work cannot slow a GPU booking down. 0 reserves nothing.
- Environments without a GPU share **all other cores** (the pool). They all get the same CPU weight, so an idle core can be used by anyone and a busy pool is split evenly. On top of that each has a hard limit: **CPU limit without GPU** (cores, default 4; 0 = the whole pool) and **Memory limit without GPU** (default 16 GB; 0 = no limit).
- **Memory pool without GPU** caps how much memory all environments without a GPU may claim together: when one starts, the memory limits of all running ones are added up, and a new one that would not fit is refused until another is paused. 0 = no total.
- SlotServe reads the number of cores and the memory of the host from Docker. If there are fewer cores than the lanes want, each lane gets its share and at least one core stays in the pool. The section on the Dev environments page shows the pool and each lane's cores. Reserved cores are assigned by logical CPU number, in lane order.

Changing these settings applies to containers created afterwards; a kept container gets its new CPU placement and limits the next time it starts.

### Usage

The CPU time and memory of every environment without a GPU are read from Docker every few seconds and recorded each minute for its owner (kind "CPU-only environments"). The **Usage** page then shows **CPU core-hours** per person.

### What admins see

The **Without a GPU** section lists every running environment without a GPU with its owner, state, CPU cores and memory in use, and a **Pause** / **Stop** button (logged in the audit log). Admins find the settings in **Admin → System settings → Dev environments**.

## Idle lanes: a shared environment

Admins choose per lane what runs while nobody has it booked (**Admin → GPUs & lanes → When not booked**):

| Mode | While the lane is not booked |
| --- | --- |
| Run the default model | The lane's default model is loaded (as before). |
| Shared dev environment | One container from a chosen template runs with the lane's GPUs. Everyone can log in. |
| Nothing | No model server and no container; the GPUs stay free. |

The shared environment is stopped ahead of the next booking (like a model preload, or a whole-GPU booking's lead time) and starts again when the lane is idle; a model does not start loading until the shared environment has let go of the GPUs. The overview and the lanes page show what an idle lane is doing.

Inside, every active SlotServe user has their own account (their user name in lower case, with characters other than letters, digits, `_` and `-` replaced), their SSH keys in `~/.ssh/authorized_keys` and, with password logins on, their environment password. Home directories live on a volume per lane and are kept. `root` cannot log in. The **Dev environments** page shows the shared environment of each idle lane with your own command:

```bash
ssh -p 2205 alice@server
```

Accounts are created when the container starts and refreshed when someone changes their keys or password (and once a minute). The image needs `useradd` or `adduser`; if it has neither the environment fails with a clear message. Only templates offered to everyone (built-in or shared by an admin) can be used.

## Storage

By default persistent data lives in Docker named volumes: `slotserve-workspace-u<user id>` for `/workspace`, `slotserve-project<id>-<path>` for a kept environment's directories, `slotserve-u<user>-t<template>-<path>` for a template's kept directories and `slotserve-idle-home-l<lane>` for the homes of a lane's shared environment.

With **Storage directory** set to a directory on the Docker host (for example `/srv/slotserve`), bind mounts are used instead:

| Data | Directory |
| --- | --- |
| `/workspace` | `<storage>/users/u<user id>/workspace` |
| A template's kept directory | `<storage>/users/u<user id>/t<template id>/<path>` |
| A kept environment's directory | `<storage>/projects/p<id>/<path>` |
| Homes of a shared environment | `<storage>/lanes/l<lane id>/home` |

The path is a path on the Docker host, not inside the SlotServe container. Missing directories are created (and filled from the image, like a new volume) by a short-lived helper container started from the environment's image, so SlotServe does not need access to the directory itself.

## What admins see

- **Dev environments** with **Everyone** switched on lists every environment with its state, image, port and errors; **Logs** shows the container output.
- A failed environment (image not found, container exited, no free port) shows the reason and can be started again with **Try again**. The person is notified.
- On restart SlotServe keeps environments that are still running, stops kept environments whose booking is over and removes leftover containers and networks.
- Shared environments: only admins can restart them or read their logs and errors (for example a failed account refresh).

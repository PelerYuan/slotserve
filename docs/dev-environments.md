# Dev environments

Besides running a model or taking the whole GPU, a booking can be a **dev environment**: for the length of the booking SlotServe runs a Docker container with the lane's GPUs and an SSH server, and you log in with your SSH key or your environment password. Your files in `/workspace` are kept between bookings, and you can keep the whole environment too. Lanes can also run a **shared environment** everyone can log into while nobody has them booked.

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
    S->>D: create the container (lane GPUs, /workspace, /models read-only)
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
5. **Jupyter or other web tools**: start them yourself and forward the port, e.g. `ssh -p 2201 -L 8888:localhost:8888 root@server`.

When the booking ends the container is removed, unless it is a kept environment. Extend the booking in time (the reminder 10 minutes before the end has buttons for that) if you need longer.

### Templates

| Template | Image |
| --- | --- |
| PyTorch | `pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel` |
| Jupyter + PyTorch | `quay.io/jupyter/pytorch-notebook:cuda12-pytorch-2.11.0` |
| TensorFlow | `tensorflow/tensorflow:2.21.0-gpu-jupyter` |
| CUDA | `nvidia/cuda:12.9.2-cudnn-devel-ubuntu24.04` |

The built-in templates use CUDA 12 builds, which run on a wide range of GPUs and drivers. Admins can change their images, switch them off or add shared templates.

Anyone can add **their own template** from any image (unless an admin turned custom images off or limited the registries), with an optional main process and environment variables. The image needs `/bin/sh`; SSH works in any image because SlotServe brings its own small static SSH server.

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
- On restart SlotServe keeps environments that are still running, stops kept environments whose booking is over and removes leftover containers.
- Shared environments: only admins can restart them or read their logs and errors (for example a failed account refresh).

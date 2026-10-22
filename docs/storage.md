# Storage

Admins add the storage dev environments get in **Admin → Storage**: a directory on the Docker host, an NFS export or an SMB/CIFS share. Each storage has a purpose, says who gets it and is checked regularly.

Without any storage nothing changes: `/workspace` and kept directories live in Docker named volumes (or under the legacy **Storage directory** setting, see [Dev environments](dev-environments.md#storage)).

## Types

| Type | What you enter | How containers get it |
| --- | --- | --- |
| Host directory | An absolute path on the machine Docker runs on, e.g. `/srv/data` | A bind mount |
| NFS | Server (host name or IP), export path (`/export/data`), optional mount options (default `nfsvers=4`) | A Docker named volume of the `local` driver with `type=nfs` |
| SMB / CIFS | Server, share (`share` or `share/folder`; `//server/share/folder` can be pasted as a whole), user name and password, optional mount options (default `vers=3.0`) | A Docker named volume of the `local` driver with `type=cifs` |

NFS and SMB are mounted by Docker itself when a container starts, so there is nothing to put in `/etc/fstab` and the same storage can later be used on other machines. The Docker host only needs the usual mount helpers:

- NFS: `nfs-common` (Debian, Ubuntu) or `nfs-utils` (RHEL, Fedora, openSUSE);
- SMB: `cifs-utils`.

Docker Desktop, rootless Docker and Podman may not be able to mount NFS or SMB volumes; use a host directory there (mounted on the host by other means) instead.

The SMB password is stored encrypted with `SLOTSERVE_SECRET_KEY` and never shown again (leave the field empty to keep it). Docker needs it in the volume's options, so anyone who can run `docker volume inspect` on the host can read it - give SlotServe a share account of its own. Because mount options are one comma-separated list, the password cannot contain commas. Mount options cannot contain `addr`, `username` or `password`, which SlotServe sets itself.

## Purposes

| Purpose | In environments | On the storage |
| --- | --- | --- |
| **Shared data** | At its mount path (e.g. `/data/team`), read-only if ticked | The whole storage |
| **Personal spaces** | Each person's folder is their `/workspace`, instead of a Docker volume | `users/u<user id>` |
| **Project data** | The kept directories of kept environments and templates | `projects/p<project id>/<path>`, `templates/u<user id>-t<template id>/<path>` |
| **Model library** | At its mount path if one is set (e.g. `/mnt/models/team`), usually read-only | Model files |

- A person's folder (and every kept directory) is created the first time it is needed and filled from the image, like a new Docker volume. A short-lived helper container of `SLOTSERVE_STORAGE_HELPER_IMAGE` does that (never the environment's own image; the image's content is copied out with Docker's archive API), so SlotServe itself needs no access to the storage. For a host directory the helper mounts only the parent folder it works in; an NFS export or SMB share is mounted from its root, since a folder that does not exist yet cannot be mounted.
- Shared idle environments (see [Dev environments](dev-environments.md#idle-lanes-a-shared-environment)) get every storage open to **everyone**, and the personal storage as a whole at `/workspaces`: each person's folder `/workspaces/u<user id>` belongs to their account there and is only open to them. Files written as `root` in a booked environment may need a `chown` to be changed from a shared environment.
- **Project data** takes over from the **Storage directory** setting for kept directories; with neither, Docker volumes are used. The setting still decides where the homes of shared environments are kept.
- **Model library**: SlotServe scans it for GGUF files (**Models → Scan folder**) and it can be picked as the target of a download (`storage_id` in `POST /api/downloads` and `POST /api/downloads/hf`). SlotServe reads and writes it at the **path SlotServe reads it at**: for a host directory that defaults to the directory itself (when SlotServe runs on the host). For NFS or SMB, mount the share on the SlotServe machine (or into the SlotServe container) and enter that path. `SLOTSERVE_MODELS_DIR` stays the default library and is still mounted at `/models`.
- When several storages have the same purpose, a person gets the first one (the oldest) they may use for personal spaces and project data, and all of them for shared data and model libraries.

## Who gets it

**Everyone**, **chosen people** or **admins only**. People who may not use a storage do not get it mounted; for personal spaces and project data they fall back to the next storage or to Docker volumes. On **Dev environments** everyone sees the storages their environments get, where they are mounted and their own usage.

Mount paths must be absolute and cannot be system directories, `/workspace`, `/workspaces`, `/models` or overlap another storage's mount path.

## Checks, usage and soft limits

**Check** mounts the storage in a short-lived container of `SLOTSERVE_STORAGE_HELPER_IMAGE` (default `busybox:1.37`, pulled when missing) and shows whether it is reachable, the error if not, free and total space (`df`) and whether it is writable. For personal spaces it also measures every person's folder (`du`); **Usage** lists them, largest first.

SlotServe checks every storage on its own every `SLOTSERVE_STORAGE_CHECK_MINUTES` (default 30; 0 = only when an admin clicks **Check**) while dev environments are on. Admins are notified when a storage that worked stops working.

A personal storage can have a **soft limit per person**: people above it get a notification (and admins one too), once until they are back under it. Nothing is blocked.

## Changing or removing a storage

Changes apply to containers started afterwards. Kept environments compare the storages they were created with: if they differ at the next booking, the container is snapshotted and recreated with the new mounts (installed software is kept). Changing how an NFS/SMB storage is mounted creates new Docker volumes; the old ones are removed once no container uses them.

Removing a storage only removes it from SlotServe; the data on it stays. Deleting a kept environment with its data also removes its folder on the project storage.

## Examples

NFS server (Linux), exporting `/srv/nfs/homes` to the Docker host `10.0.0.20`:

```bash
sudo apt install nfs-kernel-server
sudo mkdir -p /srv/nfs/homes
echo '/srv/nfs/homes 10.0.0.20(rw,sync,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

`no_root_squash` lets the helper create folders and set their owner. In SlotServe: type **NFS**, server `10.0.0.15`, export path `/srv/nfs/homes`, purpose **Personal spaces**. For NFSv3 use the options `nfsvers=3,nolock`.

SMB share on a NAS or a Samba server (`[team]` with `path = /srv/samba/team`, a user `slotserve` created with `smbpasswd -a slotserve`): type **SMB / CIFS**, server `nas.local`, share `team` (or `team/datasets`), user `slotserve` and its password, purpose **Shared data**, mount path `/data/team`, **Read-only**. CIFS has no Unix owners: files belong to `root` in the container unless you add e.g. `uid=0,gid=0,file_mode=0660,dir_mode=0770` (or `0777` for shared idle environments, whose accounts are not root). Other useful options: `vers=2.1` for older servers, `domain=STUDIO`.

To try a storage from the Docker host by hand:

```bash
docker volume create --driver local --opt type=nfs --opt o=addr=10.0.0.15,nfsvers=4 --opt device=:/srv/nfs/homes test-nfs
docker run --rm -v test-nfs:/s busybox:1.37 df -h /s
docker volume rm test-nfs
```

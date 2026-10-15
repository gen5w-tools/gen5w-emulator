# Gen5W emulator

Standalone offline QEMU emulator for the Canadian 2022 Ioniq 5 Gen5W head
unit. `emuctl` provides Docker-style lifecycle and guest control for multiple
agents. Firmware executes in detached Docker containers and survives the
calling agent's session. Each emulator has private sockets, logs, a writable
qcow2 eMMC overlay, and optional private USB media. Templates mount read-only.

## Quick start

On this VM `emuctl` is already installed on PATH, with V011 and V015 imported.

```sh
emuctl images
id=$(emuctl start V015 --name my-research)
emuctl ps --json
emuctl logs "$id" --tail 50
emuctl exec --timeout 60 --json "$id" 'getprop ro.build.display.id'
emuctl screenshot "$id" /tmp/headunit.png
emuctl tap "$id" 900 660
emuctl key "$id" 4
emuctl stop "$id"
emuctl restart "$id"
```

`start` returns when QEMU and its serial broker are available. Android may
take 1–3 minutes to boot, longer with many concurrent TCG guests. `start
--wait --timeout 300` or `wait ID --timeout 300` waits for Android's
`sys.boot_completed=1`; firmware parked at consent/setup may not reach it.
Inspect the serial log or screenshots in that case. Instances begin with the
captured prepared eMMC template state, not an empty factory filesystem.

## Commands

| Command | Purpose |
|---|---|
| `images [--json]` | List firmware templates |
| `start IMAGE [--name NAME] [--json]` | Create and boot an isolated emulator (`run` alias) |
| `ps [-a] [--json]` | List running emulators; `-a` includes stopped ones (`list` alias) |
| `inspect ID` | JSON config, paths, Docker runtime state and exact image ID |
| `exec [--timeout SECONDS] [--json] ID 'SHELL COMMAND'` | Run guest command and return its exit status |
| `logs ID [-f] [--tail N] [--source serial\|host\|gps]` | Tail guest console, QEMU/runtime output or GPS feed |
| `screenshot ID [OUTPUT] [--json]` | PNG by default, or PPM with a `.ppm` output suffix |
| `tap ID X Y` / `key ID KEYCODE` | Guest Android input commands |
| `stop ID [--timeout 15]` / `restart ID` | Stop or restart the same private disks |
| `rm ID [--force] [--purge]` | Remove container; retain artifacts unless `--purge` is given |
| `vnc attach ID [--port 5900]` / `vnc detach` / `vnc status` | Select the one optional viewer |
| `image-import NAME DIRECTORY --firmware VERSION` | Import prepared `emmc.img`, kernel and ramdisk |
| `image-verify IMAGE` | Check all template SHA-256 hashes |
| `build` | Build Docker runtime from this project |

IDs accept unique prefixes, Docker IDs, or assigned names. Ambiguous IDs fail.
Commands affect one selected emulator. Different agents can start the same
template concurrently. Commands to one guest are serialized to prevent
terminal output mixing. Guest commands execute via `sh -c`; a single quoted
argument is a shell program, multiple arguments are safely quoted as argv.
`exec --json` returns `stdout`, `exit_code`, and `error` when applicable.
The root serial console combines guest stdout/stderr and may contain
asynchronous kernel messages. Timeout returns 124 and sends Ctrl-C; client
errors return 125. Commands that reboot the guest or replace/kill the serial
shell can time out. Host `docker exec` is not the guest shell.

Useful Android tools include `getprop`, `dumpsys`, `am`, `pm`, `input`,
`uiautomator dump /data/local/tmp/ui.xml`, and `/sbin/busybox`.
For long-lived guest processes redirect output and background them explicitly.

## USB and resource isolation

```sh
emuctl start V015 --usb /path/to/usb-update.img --cpus 2 --memory 2048
emuctl start V015 --usb /path/to/usb-update.img --cpus 2 --memory 2048
```

Both starts independently snapshot the regular media file and use private
qcow2 overlays. Writes never reach the input USB file. No host block devices
or USB passthrough are accepted. The guest ramdisk mounts file-backed USB at
`/storage/usb0`. Check disk space before importing large media.

There are no fixed guest TCP ports. QMP, serial, command and GPS sockets are
unique per instance. Default RAM is 2048 MiB and SMP is 4; `--cpus` changes
guest SMP, not a Docker CPU quota. Each guest still uses host resources, so
choose concurrency to fit VM memory/CPU/disk capacity. `--resolution WIDTHxHEIGHT`
defaults to 1920x720. The emulator container has network `none`, no capabilities,
no privilege escalation, a read-only rootfs, and a single writable instance
mount. Only its selected template is mounted, read-only.

## Optional VNC

```sh
emuctl vnc attach "$id"             # switches the single viewer endpoint
emuctl vnc detach
emuctl start V011 --vnc              # attach after launch
```

Use a VNC viewer at `127.0.0.1:5900`. For remote viewing:

```sh
ssh -N -L 5900:127.0.0.1:5900 user@your-host
```

The optional relay publishes localhost only and bridges to the selected
guest's UNIX VNC socket. Attaching a different instance replaces the relay,
disconnecting previous viewers. Guest networking stays disabled. A port
already used by another application fails explicitly. Screenshots, shell,
touch, and logs need no VNC.

Android's Java-based `input` helper can take more than 30 seconds on a cold
boot under concurrent TCG load. `tap` and `key` therefore allow 120 seconds
by default; use `--timeout` to adjust. `UI READY` in the log only means the
activity service exists; wait for `sys.boot_completed` or the visible consent
screen before interacting. A black early screenshot is a booting guest.

## Installation and layout

Requires Linux, Python 3.10+, Docker, and host `qemu-img` (`qemu-utils`).
Use `./emuctl` from the checkout, symlink it onto PATH, or install this checkout
with `pip install -e .`. Build the sandbox with `emuctl build`. Imports require
prepared eMMC images. Ioniq 5 analysis docs. Firmware is private local data,
not committed here.

- `emulator/`: CLI, QMP client, serial broker, container runtime and VNC relay.
- `images/NAME/`: immutable disk/kernel/ramdisk and SHA-256 provenance manifest.
- `state/instances/ID/`: private disks, config, sockets, logs and screenshots.
- `tests/`: protocol tests; run `python3 -m unittest discover -s tests -v`.

Default state and images live in this checkout. `EMUCTL_STATE`, `EMUCTL_IMAGES`
or global `--state PATH`, `--images PATH` select alternatives. State roots have
separate Docker scopes; the one VNC relay is shared per Unix user. Socket paths
must be short enough for UNIX sockets. Keep image and state paths stable while
containers exist. Images are hash-verified at import; `image-verify` audits
them later without adding a multi-gigabyte hash pass to every launch.

The runtime uses Ubuntu 24.04 by digest. Package versions, QEMU arguments,
firmware manifest identity, and actual Docker image identity are available in
logs and `inspect`. Rebuilding may update apt packages; existing containers
keep their original runtime image. Template names are local aliases, not
OCI firmware images.

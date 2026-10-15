# Standalone emulator validation — 2026-10-13

All work was offline on the analysis VM. No hardware or physical USB access.
The old harness was moved intact to `../legacy`; a compatibility symlink at its
original analysis-project location pointed back to it. Nothing in the preserved
material was changed.

## Artifact provenance

`template-manifests.json` records exact source paths, import times, sizes and
SHA-256 hashes. Imports compared source and destination hashes and rechecked
source hashes after copying. Both `emuctl image-verify V011` and
`emuctl image-verify V015` passed after concurrent guest writes.

The captured eMMC disks had already been booted. Their hashes differ from
the legacy pristine build manifests; these templates are snapshots of that
prepared working state, not reproductions of the earlier pristine build.
V015 uses its eMMC system partition rather than the later modified loose
`legacy/run/system.ext4`. Firmware strings in the import manifests are
descriptive labels supplied at import, not freshly read vendor version
properties; the validated version lineage comes from legacy provenance.
`getprop ro.build.display.id` reports `KVT49L dev-keys` on both guests.

The Docker base is pinned by digest in the Dockerfile. The tested package
versions are QEMU `1:8.2.2+ds-0ubuntu1.18` and Python
`3.12.3-0ubuntu2.1`. See `runtime-provenance.json` for Docker image IDs,
`validation-final-inspect.json` for the final test container's full runtime
configuration, and `validation-evidence.json` for captured screenshot hashes
and validation container identities. Builds never included firmware images
in the container image or its build context.

## Verified behavior

Three guests booted together: V015 IDs `b09b7441d791` and `c58fe050387f`, and
V011 ID `c40599735aec`, each with two guest CPUs and 2048 MiB guest RAM.
The V015 pair both used the same input `legacy/usb-update.img`, independently
snapshotted into private `usb.raw` files and qcow2 writable overlays.
The final startup/timeout/VNC test used V015 ID `6b33eb978653`.

- `ps --json` listed all three concurrent guests, and `inspect` showed unique
  instance mounts and read-only template mounts. Runtime flags included
  network `none`, read-only rootfs, all capabilities dropped, no privilege
  escalation and non-root host UID/GID. Guest disks and UNIX sockets were
  private to each instance. No firmware container published TCP ports.
- `exec --json ID 'getprop ro.build.display.id; exit 7'` returned
  `{"stdout":"KVT49L dev-keys\n","exit_code":7}` with process status 7.
- Two concurrent client processes targeting the same guest returned separate
  `alpha/alpha-done` and `beta/beta-done` outputs. The guest console can still
  include asynchronous kernel messages, as documented.
- A file written in V015 B's `/data/local/tmp` was absent in V015 A.
  Both file-backed USB drives mounted at `/storage/usb0`. Writing
  `/storage/usb0/emuctl-validation.txt` in A left that file absent in B.
  Shared template hashes remained unchanged.
- `exec --timeout 1 ID '/sbin/busybox sleep 10'` returned 124. A subsequent
  `echo timeout-recovered` succeeded. The final broker returns captured
  command output on timeout without including the echoed wrapper command.
- Restarting V015 B preserved `/data/local/tmp/emuctl-persistence` containing
  `persistent-b`. The other guests continued running. `wait` observed
  `sys.boot_completed=1`.
- PNG screenshots from all three guests showed the consent UI after boot,
  including screenshots of guests with no active VNC endpoint. Early black
  screenshots were booting guests; `UI READY` preceded visible UI readiness.
- VNC attached to A, switched to B, and attached through `start --vnc` to the
  final instance. Exactly one per-user relay container published
  `127.0.0.1:5900`. A complete RFB 3.8 handshake returned security type None,
  success, a 1920x720 framebuffer, and server name `QEMU`.
- V011 `tap 900 660` and `key 4` completed with the 120-second allowance,
  producing a changed screenshot. V015 `input keyevent 4` also completed.
  Cold Java input startup exceeded the original 30-second allowance; the
  public `tap`/`key` defaults were adjusted accordingly. QMP
  `input-send-event` was never used.
- Protocol tests passed: terminal echo cannot falsely match completion
  markers, guest exit status is preserved, timeout is explicit, QMP events
  are skipped and errors propagate, and PNG encoding preserves pixels whose
  first values happen to be whitespace bytes.

Validation containers were stopped at completion; private logs, disks and
screenshots remain available under `state/instances/`. Earlier diagnostic
containers are also retained stopped, including the initial broker tests
that exposed missing guest `printf`. No other agent's emulator was stopped.

## Reproduce

```sh
python3 -m unittest discover -s tests -v
emuctl image-verify V011
emuctl image-verify V015
a=$(emuctl start V015 --cpus 2 --usb "$PWD/legacy/usb-update.img")
b=$(emuctl start V015 --cpus 2 --usb "$PWD/legacy/usb-update.img")
c=$(emuctl start V011 --cpus 2)
emuctl ps --json
emuctl wait "$a" --timeout 300
emuctl wait "$b" --timeout 300
emuctl wait "$c" --timeout 300
emuctl exec --json "$a" 'getprop ro.build.display.id; exit 7'
emuctl exec --json "$b" 'echo private-b > /data/local/tmp/isolation-proof'
emuctl exec --json "$a" '/sbin/busybox test ! -e /data/local/tmp/isolation-proof'
emuctl screenshot "$a"
emuctl screenshot "$b"
emuctl screenshot "$c"
emuctl vnc attach "$a"
emuctl vnc attach "$b"
emuctl vnc detach
emuctl stop "$a"
emuctl stop "$b"
emuctl stop "$c"
```

Tests validate the offline harness and isolation, not real head-unit update,
CAN, micom, radios or other physical hardware behavior.

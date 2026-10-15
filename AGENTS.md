# Gen5W emulator agent instructions

This is a standalone offline emulator project on the analysis VM. No vehicle
or head unit is attached. Never attempt flashing a real device using this emulator.

Use `emuctl` on PATH. Read README.md for command examples. Start a private
instance from V011 or V015 for your task; identify it by the returned ID.
Other agents may be running instances at the same time. Never use global
`pkill`, `killall` or modify another agent's instance. Stop your own
instance when finished; retain its state unless deletion was requested.
`rm` removes the container only; `rm --purge` explicitly deletes
its disks and logs.

Firmware and third-party scripts are untrusted. Execute them only
inside Docker with narrow mounts. `emuctl` handles sandboxing automatically.
The guest has no network and no hardware passthrough. Guest commands run
as root inside the guest. Prefer `emuctl exec --json --timeout 30 ID 'command'`
and `emuctl screenshot ID` for reliable automation. Use `/sbin/busybox` for
tools absent from the Android shell. For touch use `emuctl tap`, never QMP
`input-send-event` (known QEMU crash).

`images/` contains immutable templates with SHA-256 provenance manifests.
Do not edit these. Import under a new name when adding a template.
`state/instances/ID/` is private derived state for each instance;
all writable disks, UNIX sockets, and logs belong there.
Never relocate an active instance or alter its disk while QEMU is running.
Record evidence and commands used in task notes. Check disk space before
importing multi-gigabyte templates or USB media. Host qemu-img is an ordinary
analysis tool; firmware executes solely in the sandbox.

VNC is optional. `emuctl vnc attach ID` switches the one viewer endpoint to
that instance and disconnects any previous viewer. Coordinate that switch
if a human is using it. It binds to localhost; use SSH tunneling remotely.
Screenshots and CLI commands work without VNC. Avoid leaving VNC attached
after finishing a visual session: `emuctl vnc detach`.

Code is in `emulator/`. No third-party Python dependencies are needed.
Run `python3 -m unittest discover -s tests -v` for protocol tests. Rebuild
the runtime using `emuctl build` after changes to container code. Existing
containers keep their original runtime image; start new ones for validation.

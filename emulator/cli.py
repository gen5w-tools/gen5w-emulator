"""Docker-style, dependency-free client for offline firmware emulators."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import uuid
import zlib

from .protocol import guest_exec, qmp

PROJECT = Path(__file__).resolve().parent.parent
RUNTIME = "gen5w-emulator:local"
VNC_NAME = "gen5w-emulator-vnc-" + str(os.getuid())


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def docker_json(*args):
    return json.loads(run(["docker", *args], capture_output=True, text=True).stdout)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


class Client:
    def __init__(self, state, images):
        self.state = Path(state).expanduser().resolve()
        self.images = Path(images).expanduser().resolve()
        # UNIX sockets have a ~108-byte limit. Container paths are short, but
        # the host must also be able to connect to its private sockets.
        if len(str(self.state / "instances" / ("e" * 12) / "serial.sock").encode()) >= 104:
            raise ValueError("state path too long for UNIX sockets; use --state with a shorter path")
        self.scope = hashlib.sha256(str(self.state).encode()).hexdigest()[:12]

    def containers(self, all=False):
        cmd = ["docker", "ps", "--filter", f"label=emuctl.scope={self.scope}", "--format", "{{.ID}}"]
        if all:
            cmd.append("--all")
        ids = run(cmd, capture_output=True, text=True).stdout.split()
        return docker_json("inspect", *ids) if ids else []

    def resolve(self, ident, running=False):
        matches = [c for c in self.containers(True) if
                   c["Config"]["Labels"]["emuctl.id"].startswith(ident) or
                   c["Id"].startswith(ident) or c["Name"].lstrip("/") == ident or
                   c["Config"]["Labels"].get("emuctl.name") == ident]
        if len(matches) != 1:
            raise ValueError(f"emulator {ident!r}: {'ambiguous' if matches else 'not found'}")
        c = matches[0]
        if running and not c["State"]["Running"]:
            raise ValueError(f"emulator {ident!r} is stopped")
        return c, self.state / "instances" / c["Config"]["Labels"]["emuctl.id"]

    def image(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("invalid template name")
        directory = self.images / name.upper()
        manifest = json.loads((directory / "manifest.json").read_text())
        for filename in ("disk.raw", "zImage", "ramdisk.cpio.gz"):
            if not (directory / filename).is_file():
                raise ValueError(f"template missing {filename}")
            if (directory / filename).stat().st_size != manifest["files"][filename]["bytes"]:
                raise ValueError(f"template size changed: {filename}; run image-verify")
        return directory, manifest

    def start(self, a):
        template, manifest = self.image(a.image)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", a.name or "emulator"):
            raise ValueError("invalid emulator name")
        if a.cpus < 1 or a.cpus > 32 or a.memory < 256:
            raise ValueError("cpus must be 1..32 and memory at least 256 MiB")
        if not re.fullmatch(r"[1-9][0-9]{1,3}x[1-9][0-9]{1,3}", a.resolution):
            raise ValueError("resolution must be WIDTHxHEIGHT")
        with lock(self.state / "lifecycle.lock"):
            if a.name and any(c["Config"]["Labels"].get("emuctl.name") == a.name for c in self.containers(True)):
                raise ValueError(f"name {a.name!r} already exists")
            ident = uuid.uuid4().hex[:12]
            directory = self.state / "instances" / ident
            directory.mkdir(parents=True, mode=0o700)
            config = {"id": ident, "name": a.name, "image": a.image.upper(),
                      "manifest_sha256": digest(template / "manifest.json"),
                      "cpus": a.cpus, "memory": a.memory, "resolution": a.resolution,
                      "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "usb": bool(a.usb),
                      "gdb": bool(getattr(a, "gdb", False))}
            volumes = ["--mount", f"type=bind,src={template},dst=/template,readonly",
                       "--mount", f"type=bind,src={directory},dst=/instance"]
            try:
                # Only the overlay is writable. Its absolute backing path is
                # resolved within Docker, where every template mounts identically.
                run(["qemu-img", "create", "-q", "-u", "-f", "qcow2", "-F", "raw", "-b",
                     "/template/disk.raw", directory / "disk.qcow2", (template / "disk.raw").stat().st_size])
                if a.usb:
                    usb = Path(a.usb).expanduser().resolve(strict=True)
                    if not usb.is_file() or usb.is_symlink():
                        raise ValueError("USB media must be a regular image file")
                    # Snapshot input media once per instance so another writer
                    # can never change the backing bytes or compete for a lock.
                    run(["cp", "--reflink=auto", "--sparse=always", "--", usb, directory / "usb.raw"])
                    config["usb_source"] = str(usb)
                    config["usb_sha256"] = digest(directory / "usb.raw")
                    (directory / "usb.raw").chmod(0o444)
                    run(["qemu-img", "create", "-q", "-u", "-f", "qcow2", "-F", "raw", "-b",
                         "/instance/usb.raw", directory / "usb.qcow2", (directory / "usb.raw").stat().st_size])
                (directory / "config.json").write_text(json.dumps(config, indent=2) + "\n")
                name = f"emu-{self.scope}-{ident}"
                run(["docker", "run", "--detach", "--name", name,
                     "--label", f"emuctl.scope={self.scope}", "--label", f"emuctl.id={ident}",
                     "--label", f"emuctl.image={a.image.upper()}", "--label", f"emuctl.name={a.name or ''}",
                     "--network", "none", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--pids-limit", "128",
                     "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                     "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                     *volumes, RUNTIME], capture_output=True, text=True)
                for _ in range(150):
                    if (directory / "exec.sock").exists():
                        break
                    if not docker_json("inspect", name)[0]["State"]["Running"]:
                        raise RuntimeError("emulator failed to launch; inspect with emuctl logs --source host " + ident)
                    time.sleep(0.1)
                else:
                    raise RuntimeError("serial broker not ready; inspect emulator " + ident)
            except Exception:
                # Keep artifacts and any failed container for diagnosis. No
                # automatic deletion of user state or unrelated emulators.
                print(f"startup artifacts: {directory}", file=sys.stderr)
                raise
        print(json.dumps(config) if a.json else ident, flush=True)
        if a.wait:
            self.wait(ident, a.timeout)
        if a.vnc:
            self.vnc_attach(ident, a.vnc_port)

    def wait(self, ident, timeout):
        _, directory = self.resolve(ident, True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                with lock(directory / "exec.lock"):
                    reply = guest_exec(directory / "exec.sock", "getprop sys.boot_completed", min(10, max(1, end-time.monotonic())))
                if reply["exit_code"] == 0 and reply["stdout"].strip() == "1":
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError(f"{ident}: Android boot_completed not reached within {timeout}s; use logs to inspect")

    def vnc_attach(self, ident, port):
        _, directory = self.resolve(ident, True)
        if not 1 <= port <= 65535:
            raise ValueError("invalid VNC port")
        with lock(Path(f"/tmp/emuctl-vnc-{os.getuid()}.lock")):
            existing = run(["docker", "ps", "--all", "--filter", f"name=^/{VNC_NAME}$", "--format", "{{.ID}}"], capture_output=True, text=True).stdout.strip()
            if existing:
                run(["docker", "rm", "--force", existing], capture_output=True)
            qmp(directory / "qmp.sock", "display-update", {"type": "vnc", "addresses": [{"type": "unix", "path": "/instance/vnc.sock"}]})
            if not (directory / "vnc.sock").exists():
                raise RuntimeError("QEMU did not enable its VNC socket")
            run(["docker", "run", "--detach", "--name", VNC_NAME,
                 "--label", f"emuctl.vnc.id={directory.name}", "--label", f"emuctl.vnc.scope={self.scope}",
                 "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--pids-limit", "64", "--user", f"{os.getuid()}:{os.getgid()}",
                 "--publish", f"127.0.0.1:{port}:5900", "--mount", f"type=bind,src={directory},dst=/instance,readonly",
                 "--entrypoint", "python3", RUNTIME, "/opt/emulator/relay.py", "/instance/vnc.sock"], capture_output=True)
        print(f"VNC {directory.name}: 127.0.0.1:{port} (SSH tunnel for remote access)", file=sys.stderr)

    def vnc_detach(self):
        with lock(Path(f"/tmp/emuctl-vnc-{os.getuid()}.lock")):
            found = run(["docker", "ps", "--all", "--filter", f"name=^/{VNC_NAME}$", "--format", "{{.ID}}"], capture_output=True, text=True).stdout.strip()
            if found:
                run(["docker", "rm", "--force", found], capture_output=True)

    def vnc_detach_instance(self, ident):
        with lock(Path(f"/tmp/emuctl-vnc-{os.getuid()}.lock")):
            found = run(["docker", "ps", "--all", "--filter", f"name=^/{VNC_NAME}$",
                         "--filter", f"label=emuctl.vnc.id={ident}",
                         "--filter", f"label=emuctl.vnc.scope={self.scope}",
                         "--format", "{{.ID}}"], capture_output=True, text=True).stdout.strip()
            if found:
                run(["docker", "rm", "--force", found], capture_output=True)


def png_from_ppm(source, target):
    data = source.read_bytes()
    pos = 0
    tokens = []
    while len(tokens) < 4:
        while data[pos:pos+1].isspace():
            pos += 1
        if data[pos:pos+1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while end < len(data) and not data[end:end+1].isspace():
            end += 1
        tokens.append(data[pos:end])
        pos = end
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError("expected an 8-bit P6 framebuffer")
    width, height = map(int, tokens[1:3])
    pos += 2 if data[pos:pos+2] == b"\r\n" else 1
    pixels = data[pos:]
    if len(pixels) != width * height * 3:
        raise ValueError("framebuffer length mismatch")
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    rows = b"".join(b"\0" + pixels[i:i+width*3] for i in range(0, len(pixels), width*3))
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
                       chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def parser():
    p = argparse.ArgumentParser(description="Concurrent offline Gen5W emulators. IDs accept unique prefixes; --json supports agents.")
    p.add_argument("--version", action="version", version="emuctl 0.1.0")
    p.add_argument("--state", default=os.environ.get("EMUCTL_STATE", str(PROJECT / "state")))
    p.add_argument("--images", default=os.environ.get("EMUCTL_IMAGES", str(PROJECT / "images")))
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("build", help="build the sandbox runtime")
    images = sub.add_parser("images", help="list immutable firmware templates")
    images.add_argument("--json", action="store_true")
    imp = sub.add_parser("image-import", help="snapshot a prepared eMMC emulator directory")
    imp.add_argument("name"); imp.add_argument("source"); imp.add_argument("--firmware", required=True)
    verify = sub.add_parser("image-verify", help="verify template SHA-256 hashes")
    verify.add_argument("image")
    ps = sub.add_parser("ps", aliases=["list"], help="list running emulators")
    ps.add_argument("-a", "--all", action="store_true"); ps.add_argument("--json", action="store_true")
    start = sub.add_parser("start", aliases=["run"], help="start a new isolated instance from a template")
    start.add_argument("image"); start.add_argument("--name"); start.add_argument("--json", action="store_true")
    start.add_argument("--cpus", type=int, default=4); start.add_argument("--memory", type=int, default=2048)
    start.add_argument("--resolution", default="1920x720"); start.add_argument("--usb", help="private snapshot of FILE-backed USB media")
    start.add_argument("--wait", action="store_true"); start.add_argument("--timeout", type=float, default=180)
    start.add_argument("--vnc", action="store_true"); start.add_argument("--vnc-port", type=int, default=5900)
    start.add_argument("--gdb", action="store_true", help="pause QEMU at reset (-S) and expose a gdbstub on unix:/instance/gdb.sock (attach: gdb-multiarch -ex 'target remote | socat - UNIX-CONNECT:<inst>/gdb.sock' -ex continue)")
    for action in ("inspect", "stop", "restart", "rm", "wait"):
        s = sub.add_parser(action)
        s.add_argument("id")
        if action in ("stop", "wait"):
            s.add_argument("--timeout", type=int, default=180 if action == "wait" else 15)
        if action == "rm":
            s.add_argument("--force", action="store_true"); s.add_argument("--purge", action="store_true", help="also delete private disks/logs")
    logs = sub.add_parser("logs")
    logs.add_argument("id"); logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--tail", type=int, default=100); logs.add_argument("--source", choices=["serial", "host", "gps"], default="serial")
    ex = sub.add_parser("exec", help="run a guest shell command; propagate its exit status")
    ex.add_argument("--timeout", type=float, default=30); ex.add_argument("--json", action="store_true")
    ex.add_argument("id"); ex.add_argument("command", nargs=argparse.REMAINDER)
    shot = sub.add_parser("screenshot")
    shot.add_argument("id"); shot.add_argument("output", nargs="?"); shot.add_argument("--json", action="store_true")
    tap = sub.add_parser("tap"); tap.add_argument("id"); tap.add_argument("x", type=int); tap.add_argument("y", type=int)
    tap.add_argument("--timeout", type=float, default=120)
    key = sub.add_parser("key"); key.add_argument("id"); key.add_argument("keycode", type=int)
    key.add_argument("--timeout", type=float, default=120)
    vnc = sub.add_parser("vnc"); vs = vnc.add_subparsers(dest="vnc_action", required=True)
    attach = vs.add_parser("attach"); attach.add_argument("id"); attach.add_argument("--port", type=int, default=5900)
    vs.add_parser("detach"); vs.add_parser("status")
    return p


def dispatch(a, c):
    if a.action == "build":
        run(["docker", "build", "-t", RUNTIME, PROJECT])
    elif a.action == "image-import":
        name = a.name.upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", name):
            raise ValueError("invalid template name")
        source = Path(a.source).expanduser().resolve(strict=True)
        with lock(c.images / ".import.lock"):
            destination = c.images / name
            if destination.exists():
                raise ValueError("template already exists; import with a new name")
            # Avoid publishing an incomplete template to concurrent starts.
            temporary = c.images / (".import-" + uuid.uuid4().hex)
            temporary.mkdir()
            files = {}
            for src, dst in (("emmc.img", "disk.raw"), ("zImage", "zImage"), ("ramdisk.cpio.gz", "ramdisk.cpio.gz")):
                if not (source / src).is_file():
                    raise ValueError(f"source must be a regular file: {source / src}")
                before = digest(source / src)
                run(["cp", "--reflink=auto", "--sparse=always", "--", source / src, temporary / dst])
                actual = digest(temporary / dst)
                if actual != before or digest(source / src) != before:
                    raise RuntimeError("source changed during import; stop its emulator first")
                (temporary / dst).chmod(0o444)
                files[dst] = {"sha256": actual, "bytes": (temporary / dst).stat().st_size, "source": str(source / src)}
            manifest = {"name": name, "firmware": a.firmware, "imported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": files,
                        "profile": "emmc", "source_note": "Prepared third-party offline QEMU eMMC profile; guest state as captured."}
            (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (temporary / "manifest.json").chmod(0o444)
            temporary.rename(destination)
        print(name)
    elif a.action == "image-verify":
        directory, manifest = c.image(a.image)
        for name, metadata in manifest["files"].items():
            if digest(directory / name) != metadata["sha256"]:
                raise RuntimeError(f"hash mismatch: {name}")
        print(f"{manifest['name']}: all SHA-256 hashes verified")
    elif a.action == "images":
        rows = [json.loads(p.read_text()) for p in sorted(c.images.glob("*/manifest.json")) if not p.parent.name.startswith(".")]
        if a.json:
            print(json.dumps(rows))
        else:
            print("IMAGE\tFIRMWARE\tPROFILE")
            for r in rows:
                print(f"{r['name']}\t{r['firmware']}\t{r['profile']}")
    elif a.action in ("ps", "list"):
        rows = [{"id": x["Config"]["Labels"]["emuctl.id"], "name": x["Config"]["Labels"].get("emuctl.name"),
                 "image": x["Config"]["Labels"]["emuctl.image"], "status": x["State"]["Status"],
                 "container_id": x["Id"], "created": x["Created"]} for x in c.containers(a.all)]
        if a.json:
            print(json.dumps(rows))
        else:
            print("ID\tIMAGE\tSTATUS\tNAME")
            for r in rows:
                print(f"{r['id']}\t{r['image']}\t{r['status']}\t{r['name'] or ''}")
    elif a.action in ("start", "run"):
        c.start(a)
    elif a.action == "vnc":
        if a.vnc_action == "attach":
            c.vnc_attach(a.id, a.port)
        elif a.vnc_action == "detach":
            c.vnc_detach()
        else:
            run(["docker", "ps", "--all", "--filter", f"name=^/{VNC_NAME}$", "--format", "{{json .}}"])
    else:
        container, directory = c.resolve(a.id, a.action in ("exec", "tap", "key", "screenshot", "wait"))
        cid = container["Id"]
        if a.action == "inspect":
            print(json.dumps({"config": json.loads((directory / "config.json").read_text()), "directory": str(directory),
                              "docker": container}, indent=2))
        elif a.action == "wait":
            c.wait(a.id, a.timeout)
        elif a.action in ("stop", "restart", "rm"):
            with lock(c.state / "lifecycle.lock"):
                if a.action == "rm" and container["State"]["Running"] and not a.force:
                    raise ValueError("stop the emulator before rm, or explicitly use --force")
                if a.action in ("stop", "rm"):
                    c.vnc_detach_instance(directory.name)
                args = ["docker", a.action]
                if a.action == "stop":
                    args += ["--time", str(a.timeout)]
                if a.action == "rm" and a.force:
                    args += ["--force"]
                run([*args, cid], capture_output=True)
                if a.action == "rm" and a.purge:
                    shutil.rmtree(directory)
        elif a.action == "logs":
            if a.source == "host":
                run(["docker", "logs", "--tail", str(a.tail), *(["--follow"] if a.follow else []), cid])
            else:
                run(["tail", "-n", str(a.tail), *(["-F"] if a.follow else []), directory / (a.source + ".log")])
        elif a.action in ("exec", "tap", "key"):
            if a.action == "exec":
                words = a.command
                if words[:1] == ["--"]:
                    words = words[1:]
                if not words:
                    raise ValueError("guest command required")
                command = words[0] if len(words) == 1 else shlex.join(words)
            else:
                command = f"input tap {a.x} {a.y}" if a.action == "tap" else f"input keyevent {a.keycode}"
            # Host lock prevents queued requests from consuming their socket
            # timeout while another agent's command is still running.
            with lock(directory / "exec.lock"):
                reply = guest_exec(directory / "exec.sock", command, getattr(a, "timeout", 30))
            if getattr(a, "json", False):
                print(json.dumps(reply))
            else:
                print(reply.get("stdout", ""), end="")
                if reply.get("error"):
                    print(reply["error"], file=sys.stderr)
            return reply["exit_code"]
        elif a.action == "screenshot":
            captures = directory / "screenshots"
            captures.mkdir(exist_ok=True)
            token = uuid.uuid4().hex
            raw = captures / (token + ".ppm")
            qmp(directory / "qmp.sock", "screendump", {"filename": "/instance/screenshots/" + raw.name})
            output = Path(a.output).expanduser().resolve() if a.output else captures / (token + ".png")
            if output.suffix.lower() == ".ppm":
                shutil.copyfile(raw, output)
            else:
                png_from_ppm(raw, output)
            print(json.dumps({"path": str(output), "bytes": output.stat().st_size}) if a.json else output)
    return 0


def main():
    args = parser().parse_args()
    try:
        raise SystemExit(dispatch(args, Client(args.state, args.images)))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"emuctl: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr, file=sys.stderr)
        raise SystemExit(125)


if __name__ == "__main__":
    main()

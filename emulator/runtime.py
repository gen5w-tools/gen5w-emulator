"""Runs inside Docker. All guest state and services are private to /instance."""
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import uuid

INSTANCE = Path("/instance")


class SerialBroker:
    def __init__(self, serial, directory):
        self.serial = serial
        self.directory = directory
        self.cv = threading.Condition()
        self.buffer = bytearray()
        self.dead = False

    def read(self):
        with (self.directory / "serial.log").open("ab", buffering=0) as log:
            try:
                while True:
                    data = self.serial.recv(65536)
                    if not data:
                        break
                    log.write(data)
                    with self.cv:
                        self.buffer.extend(data)
                        # Requests capture at most 8 MiB; logs remain complete.
                        if len(self.buffer) > 8 * 1024 * 1024:
                            del self.buffer[:len(self.buffer) - 8 * 1024 * 1024]
                        self.cv.notify_all()
            finally:
                with self.cv:
                    self.dead = True
                    self.cv.notify_all()

    def execute(self, command, timeout):
        token = uuid.uuid4().hex
        begin = "EMU_BEGIN_" + token
        end = "EMU_END_" + token
        # The full markers never occur in the terminal's echoed command line.
        line = ("/sbin/busybox printf '\\n%s%s\\n' EMU_BEGIN_ " + token + "; sh -c " +
                shlex.quote(command) + "; rc=$?; /sbin/busybox printf '\\n%s%s:%s\\n' EMU_END_ " +
                token + ' "$rc"\r')
        deadline = time.monotonic() + timeout
        with self.cv:
            self.buffer.clear()
            self.serial.sendall(line.encode())
            while True:
                data = bytes(self.buffer).replace(b"\r", b"")
                start = data.find(("\n" + begin + "\n").encode())
                match = re.search(rb"\n" + end.encode() + rb":([0-9]+)\n", data)
                if start >= 0 and match and match.start() > start:
                    output = data[start + len(begin) + 2:match.start()]
                    return {"stdout": output.decode("utf-8", "replace"), "exit_code": int(match[1])}
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.dead:
                    if not self.dead:
                        self.serial.sendall(b"\x03")
                    output = data[start + len(begin) + 2:] if start >= 0 else b""
                    return {"stdout": output.decode("utf-8", "replace"), "exit_code": 124,
                            "error": "guest command timed out or serial disconnected"}
                self.cv.wait(remaining)

    def serve(self):
        threading.Thread(target=self.read, daemon=True).start()
        path = self.directory / "exec.sock"
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen(32)
            while not self.dead:
                client, _ = server.accept()
                with client:
                    try:
                        client.settimeout(10)
                        with client.makefile("rb") as stream:
                            req = json.loads(stream.readline(1024 * 1024))
                        timeout = min(3600, max(1, float(req.get("timeout", 30))))
                        command = req["command"]
                        if "\x00" in command or "\r" in command:
                            raise ValueError("NUL and CR are not supported in commands")
                        reply = self.execute(command, timeout)
                        client.sendall(json.dumps(reply).encode() + b"\n")
                    except (ValueError, KeyError, OSError) as exc:
                        try:
                            client.sendall(json.dumps({"error": str(exc), "stdout": "", "exit_code": 125}).encode() + b"\n")
                        except OSError:
                            pass


def main():
    os.chdir(INSTANCE)
    config = json.loads(Path("config.json").read_text())
    x, y = config["resolution"].split("x")
    args = ["qemu-system-arm", "-M", "virt,highmem=off", "-cpu", "cortex-a7",
            "-m", str(config["memory"]), "-smp", str(config["cpus"]),
            "-accel", "tcg,thread=multi,tb-size=512", "-global", "virtio-mmio.force-legacy=false",
            "-kernel", "/template/zImage", "-initrd", "/template/ramdisk.cpio.gz",
            "-drive", "file=disk.qcow2,if=none,format=qcow2,id=emmc",
            "-device", "virtio-blk-device,drive=emmc,serial=bsp_mmc",
            "-device", f"bochs-display,xres={x},yres={y},romfile=",
            "-device", "virtio-tablet-device,id=touch0", "-device", "intel-hda",
            "-device", "hda-output,audiodev=snd", "-audiodev", "none,id=snd",
            "-display", "none", "-nic", "none", "-vnc", "none",
            "-qmp", "unix:qmp.sock,server=on,wait=off",
            "-chardev", "socket,id=ser0,path=serial.sock,server=on,wait=off", "-serial", "chardev:ser0",
            "-chardev", "socket,id=gpsser,path=gps.sock,server=on,wait=off", "-device", "pci-serial,chardev=gpsser",
            "-append", f"video={x}x{y}-32@60 vmalloc=512M console=ttyAMA0 androidboot.hardware=tcc803x rdinit=/init_wrap.sh"]
    if config.get("usb"):
        args.extend(["-drive", "file=usb.qcow2,if=none,format=qcow2,id=usbdisk",
                     "-device", "qemu-xhci,id=usb0", "-device", "usb-storage,bus=usb0.0,drive=usbdisk,removable=on"])
    if config.get("gdb"):
        # debug aid: start paused at reset, expose a gdbstub on a unix socket the
        # host can reach through the bind-mounted instance dir.
        args.extend(["-gdb", "unix:/instance/gdb.sock,server=on,wait=off", "-S"])
    for name in ("qmp.sock", "serial.sock", "gps.sock", "exec.sock", "vnc.sock", "gdb.sock"):
        Path(name).unlink(missing_ok=True)
    print(json.dumps({"qemu_argv": args, "config": config}), flush=True)
    subprocess.run(["qemu-system-arm", "--version"], check=True)
    qemu = subprocess.Popen(args)
    gps = subprocess.Popen(["python3", "/opt/emulator/gps_feed.py", "gps.sock"],
                           stdout=Path("gps.log").open("ab"), stderr=subprocess.STDOUT)
    def stop(*_):
        qemu.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for _ in range(300):
            if qemu.poll() is not None:
                raise RuntimeError("QEMU exited during startup")
            try:
                serial = socket.socket(socket.AF_UNIX)
                serial.connect("serial.sock")
                break
            except OSError:
                serial.close()
                time.sleep(0.1)
        else:
            raise RuntimeError("serial socket did not become ready")
        broker = SerialBroker(serial, INSTANCE)
        threading.Thread(target=broker.serve, daemon=True).start()
        code = qemu.wait()
    finally:
        gps.terminate()
        gps.wait(timeout=10)
        if qemu.poll() is None:
            qemu.terminate()
            qemu.wait(timeout=10)
    raise SystemExit(code)


if __name__ == "__main__":
    main()

"""Small bounded JSON protocols used by the host client and container broker."""
import json
import socket


def qmp(path, command, arguments=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(15)
        sock.connect(str(path))
        with sock.makefile("rwb") as stream:
            if not stream.readline():
                raise RuntimeError("QMP greeting missing")
            def request(cmd, args=None):
                payload = {"execute": cmd}
                if args is not None:
                    payload["arguments"] = args
                stream.write(json.dumps(payload).encode() + b"\n")
                stream.flush()
                while True:
                    line = stream.readline()
                    if not line:
                        raise RuntimeError("QMP closed")
                    reply = json.loads(line)
                    if "error" in reply:
                        raise RuntimeError(str(reply["error"]))
                    if "return" in reply:
                        return reply["return"]
            request("qmp_capabilities")
            return request(command, arguments)


def guest_exec(path, command, timeout):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout + 20)
        sock.connect(str(path))
        sock.sendall(json.dumps({"command": command, "timeout": timeout}).encode() + b"\n")
        with sock.makefile("rb") as stream:
            line = stream.readline(16 * 1024 * 1024)
            if not line:
                raise RuntimeError("serial broker closed")
            return json.loads(line)

"""qRcmd 旁路：识别 `$qRcmd,...#xx`，命中后本地处理并回包。"""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Optional

_QRCMD_MARK = b"$qRcmd,"
_TAIL_KEEP = len(_QRCMD_MARK) - 1
_BASE_DIR = Path(__file__).resolve().parent
_SCRIPT_DIR = Path(os.environ.get("GDB_PROXY_SCRIPT_DIR", str(_BASE_DIR / "scripts")))
_SCRIPT_TIMEOUT_SEC = 60
_O_CHUNK = 384

# 当前只启用 tpu_hang_info；后续扩展只需在这里增加映射。
_SCRIPT_REGISTRY = {
    "tpu_hang_info": "tpu_hang_info_host.sh",
}

MonitorBypass = Callable[[int, bytes, bytes], Optional[bytes]]


def rsp_packet(payload: str) -> bytes:
    csum = sum(ord(c) for c in payload) & 0xFF
    return f"${payload}#{csum:02x}".encode("ascii")


def rsp_qrcmd(cmd: bytes) -> bytes:
    if not cmd:
        raise ValueError("rsp_qrcmd: empty command")
    return rsp_packet(f"qRcmd,{cmd.hex()}")


def _monitor_reply(text: str) -> bytes:
    buf = bytearray()
    raw = text.encode("utf-8", errors="replace")
    for i in range(0, len(raw), _O_CHUNK):
        buf.extend(rsp_packet("O" + raw[i : i + _O_CHUNK].hex()))
    buf.extend(rsp_packet("OK"))
    return bytes(buf)


def _decode_monitor_text(monitor_cmd: bytes) -> str:
    text = monitor_cmd.decode("utf-8", errors="replace").strip()
    if text.startswith("monitor "):
        text = text[len("monitor ") :].strip()
    return text


def _run_script(script_name: str, script_args: list[str]) -> bytes:
    rel_path = _SCRIPT_REGISTRY.get(script_name)
    if rel_path is None:
        return _monitor_reply(f"ERR: unsupported script '{script_name}'\n")

    script_path = (_SCRIPT_DIR / rel_path).resolve()
    if not script_path.is_file():
        return _monitor_reply(f"ERR: {script_path} not found\n")

    if rel_path.endswith(".sh"):
        # login shell：加载 /etc/profile.d，使 test_reg 等 TPU 工具在 PATH 中可用。
        shell_cmd = shlex.quote(str(script_path))
        if script_args:
            shell_cmd += " " + " ".join(shlex.quote(a) for a in script_args)
        cmd = ["bash", "-lc", shell_cmd]
    elif rel_path.endswith(".py"):
        cmd = ["python3", str(script_path)] + script_args
    else:
        return _monitor_reply(f"ERR: unsupported script type: {rel_path}\n")
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_SCRIPT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _monitor_reply(f"ERR: timeout after {_SCRIPT_TIMEOUT_SEC}s\n")
    except OSError as exc:
        return _monitor_reply(f"ERR: {exc}\n")

    output = proc.stdout or ""
    if proc.returncode != 0:
        if output and not output.endswith("\n"):
            output += "\n"
        output += f"ERR: exit={proc.returncode}\n"
    return _monitor_reply(output)


def _handle_script_command(core_id: int, text: str) -> Optional[bytes]:
    if not text.startswith("script"):
        return None

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        return _monitor_reply(f"ERR: bad command format: {exc}\n")

    if len(tokens) == 1:
        supported = " ".join(sorted(_SCRIPT_REGISTRY.keys()))
        return _monitor_reply(f"SUPPORTED: {supported}\n")

    script_name = tokens[1]
    script_args = tokens[2:]
    print(
        f"[proxy] script core={core_id} name={script_name} args={script_args!r}",
        flush=True,
    )
    return _run_script(script_name, script_args)


def default_monitor_bypass(
    core_id: int, monitor_cmd: bytes, raw_qrcmd: bytes
) -> Optional[bytes]:
    text = _decode_monitor_text(monitor_cmd)
    return _handle_script_command(core_id, text)


monitor_bypass_handler: MonitorBypass = default_monitor_bypass


def filter_h2d_stream(
    buf: bytearray,
    data: bytes,
    core_id: int,
    send_to_gdb: Callable[[bytes], None],
) -> bytes:
    if not buf and _QRCMD_MARK not in data:
        return data

    buf.extend(data)
    out = bytearray()
    while True:
        start = buf.find(_QRCMD_MARK)
        if start < 0:
            if len(buf) > _TAIL_KEEP:
                out.extend(buf[:-_TAIL_KEEP])
                del buf[:-_TAIL_KEEP]
            break

        if start > 0:
            out.extend(buf[:start])
            del buf[:start]

        sharp = buf.find(b"#", len(_QRCMD_MARK))
        if sharp < 0 or sharp + 2 >= len(buf):
            break

        packet = bytes(buf[: sharp + 3])
        hexpart = packet[len(_QRCMD_MARK) : sharp]
        del buf[: sharp + 3]

        try:
            cmd = bytes.fromhex(hexpart.decode("ascii"))
        except ValueError:
            out.extend(packet)
            continue

        reply = monitor_bypass_handler(core_id, cmd, packet)
        if reply is None:
            out.extend(packet)
        else:
            send_to_gdb(reply)

    return bytes(out)

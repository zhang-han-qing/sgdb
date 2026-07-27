"""tpu-attach command implementation."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import gdb

from plugins.commands.tp_attach import attach_core, build_endpoint
from plugins.devices.registry import get_device
from plugins.devices.registry import list_supported_device_names
from plugins.state.tpu_threads import reset_tpu_thread_bindings
from plugins.state.tpu_threads import set_tpu_thread_binding


def _parse_attach_arg(arg: str) -> tuple[str, int]:
    parts = arg.strip().split()
    if len(parts) != 2:
        raise gdb.GdbError("用法: tpu-attach <device-type> <device-id>（例如: tpu-attach 1690 0）")

    device_type, device_id_raw = parts
    if get_device(device_type) is None:
        supported = ", ".join(list_supported_device_names())
        raise gdb.GdbError(f"不支持设备类型: {device_type}（当前支持: {supported}）")
    try:
        device_id = int(device_id_raw)
    except ValueError as exc:
        raise gdb.GdbError(f"device-id 必须是非负整数，当前: {device_id_raw}") from exc
    if device_id < 0:
        raise gdb.GdbError(f"device-id 必须是非负整数，当前: {device_id}")
    return device_type, device_id


def _cache_device_dir(device_name: str) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / ".sgdb_cache" / "sysroots" / device_name


def _last_ip_file(device_key: str) -> Path:
    return _cache_device_dir(device_key) / "last_ip"


def _load_last_ip(device_key: str, fallback_ip: str) -> str:
    ip_file = _last_ip_file(device_key)
    if not ip_file.is_file():
        return fallback_ip
    text = ip_file.read_text(encoding="utf-8").strip()
    return text or fallback_ip


def _save_last_ip(device_key: str, ip: str) -> None:
    ip_file = _last_ip_file(device_key)
    ip_file.parent.mkdir(parents=True, exist_ok=True)
    ip_file.write_text(f"{ip}\n", encoding="utf-8")


def _resolve_target_ip(device_key: str, default_ip: str) -> str:
    remembered_ip = _load_last_ip(device_key, default_ip)
    user_ip = input(f"[tpu-attach] target ip (default: {remembered_ip}): ").strip()
    resolved_ip = user_ip or remembered_ip
    if not resolved_ip:
        raise gdb.GdbError("target ip 不能为空")
    _save_last_ip(device_key, resolved_ip)
    return resolved_ip


def _find_cached_rootfs(device_name: str) -> Path | None:
    current_rootfs = _cache_device_dir(device_name) / "current" / "rootfs"
    if current_rootfs.is_dir():
        return current_rootfs.resolve()
    return None


def _fetch_cpio(remote: str, remote_rootfs_cpio: str, output_path: Path) -> None:
    if remote:
        remote_spec = f"{remote}:{remote_rootfs_cpio}"
        try:
            subprocess.run(["scp", remote_spec, str(output_path)], check=True)
        except FileNotFoundError as exc:
            raise gdb.GdbError("未找到 scp 命令，请先安装 openssh-client") from exc
        except subprocess.CalledProcessError as exc:
            raise gdb.GdbError(f"SCP 拉取失败: {remote_spec} (exit={exc.returncode})") from exc
        return

    local_cpio = Path(remote_rootfs_cpio)
    if not local_cpio.exists():
        raise gdb.GdbError(f"本地未找到 sysroot 包: {local_cpio}")
    shutil.copy2(local_cpio, output_path)


def _md5sum_file(file_path: Path) -> str:
    digest = hashlib.md5()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_cpio_to_dir(cpio_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("bsdtar"):
        subprocess.run(["bsdtar", "-xf", str(cpio_path)], check=True, cwd=output_dir)
        return
    if shutil.which("cpio"):
        with cpio_path.open("rb") as f:
            subprocess.run(
                ["cpio", "-idmu", "--no-absolute-filenames"],
                check=True,
                cwd=output_dir,
                stdin=f,
            )
        return
    raise gdb.GdbError("解压 tp_rootfs.cpio 失败：未找到 bsdtar/cpio 工具")


def _set_current_symlink(device_cache_dir: Path, version_dir: Path) -> None:
    current_link = device_cache_dir / "current"
    if current_link.is_symlink() or current_link.exists():
        current_link.unlink()
    current_link.symlink_to(version_dir.resolve(), target_is_directory=True)


def _resolve_sysroot(device_name: str, remote_rootfs_cpio: str | None) -> Path | None:
    device_cache_dir = _cache_device_dir(device_name)
    device_cache_dir.mkdir(parents=True, exist_ok=True)

    if remote_rootfs_cpio is None:
        print(f"[tpu-attach] {device_name} 未配置 rootfs CPIO，跳过 sysroot 设置。")
        return None

    if not input("[tpu-attach] update sysroot from remote? (y/N): ").strip().lower() in {"y", "yes"}:
        cached_rootfs = _find_cached_rootfs(device_name)
        if cached_rootfs is None:
            print("[tpu-attach] skip sysroot setting.")
            return None
        print(f"[tpu-attach] use cached sysroot: {cached_rootfs}")
        return cached_rootfs

    temp_cpio = device_cache_dir / "tp_rootfs.download.cpio"
    try:
        remote = input("[tpu-attach] remote machine name@ip (empty for local): ").strip()
        _fetch_cpio(remote, remote_rootfs_cpio, temp_cpio)
        md5_value = _md5sum_file(temp_cpio)
        version_dir = device_cache_dir / md5_value
        version_rootfs = version_dir / "rootfs"

        if not version_rootfs.is_dir():
            version_dir.mkdir(parents=True, exist_ok=True)
            temp_extract_dir = version_dir / "rootfs.tmp"
            if temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir)
            _extract_cpio_to_dir(temp_cpio, temp_extract_dir)
            if version_rootfs.exists():
                shutil.rmtree(version_rootfs)
            temp_extract_dir.rename(version_rootfs)
            print(f"[tpu-attach] cache new sysroot: {version_rootfs}")
        else:
            print(f"[tpu-attach] reuse cached sysroot: {version_rootfs}")

        _set_current_symlink(device_cache_dir, version_dir)
        return version_rootfs
    finally:
        temp_cpio.unlink(missing_ok=True)


def _apply_sysroot(rootfs_dir: Path) -> None:
    gdb.execute(f"set sysroot {rootfs_dir}", to_string=True)
    solib_dirs = [str(path) for path in (rootfs_dir / "lib", rootfs_dir / "usr/lib") if path.is_dir()]
    if solib_dirs:
        gdb.execute(f"set solib-search-path {':'.join(solib_dirs)}", to_string=True)
    print(f"[tpu-attach] sysroot set: {rootfs_dir}")


class TPUAttachCommand(gdb.Command):
    """tpu-attach <device-type> <device-id>: connect TP cores and attach the registered process."""

    def __init__(self) -> None:
        super().__init__("tpu-attach", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        device_type, device_id = _parse_attach_arg(arg)
        device = get_device(device_type)
        if device is None:
            supported = ", ".join(list_supported_device_names())
            raise gdb.GdbError(f"不支持设备类型: {device_type}。当前支持: {supported}")
        device_key = f"{device_type}-{device_id}"

        initial_inferiors = sorted(inf.num for inf in gdb.inferiors())
        if initial_inferiors != [1]:
            print(
                f"[tpu-attach] warning: 当前 inferiors={initial_inferiors}，"
                "仅支持初始状态为 inferior 1，已退出。"
            )
            return

        resolved_sysroot = _resolve_sysroot(
            device_name=device.name,
            remote_rootfs_cpio=device.remote_rootfs_cpio,
        )
        if resolved_sysroot is not None:
            _apply_sysroot(resolved_sysroot)

        target_ip = _resolve_target_ip(device_key=device_key, default_ip=device.ip)

        try:
            gdb.execute("set mi-async on", to_string=True)
        except Exception:  # noqa: BLE001
            print("[tpu-attach] warning: 当前 GDB 不支持 set mi-async on，已跳过。")
        gdb.execute("set non-stop on", to_string=True)
        gdb.execute("set schedule-multiple on", to_string=True)

        for _ in range(device.core_num - 1):
            gdb.execute("add-inferior", to_string=True)
        inferiors_after_add = sorted(inf.num for inf in gdb.inferiors())
        expected = list(range(1, device.core_num + 1))
        if inferiors_after_add != expected:
            raise gdb.GdbError(
                f"inferior 编号检查失败，期望 {expected}，实际 {inferiors_after_add}"
            )

        results: list[str] = []
        failures: list[str] = []
        reset_tpu_thread_bindings()
        for core_id in range(device.core_num):
            inferior_num = core_id + 1
            try:
                gdb.execute(f"inferior {inferior_num}", to_string=True)
                endpoint, pid = attach_core(
                    device_type=device_type,
                    device_id=device_id,
                    core_id=core_id,
                    ip=target_ip,
                )
                set_tpu_thread_binding(tp_name=f"tp{core_id}", inferior_num=inferior_num)
                results.append(f"inferior {inferior_num} <- {endpoint}, pid={pid}")
            except Exception as exc:  # noqa: BLE001
                endpoint = build_endpoint(ip=target_ip, device_id=device_id, core_id=core_id)
                failures.append(f"{endpoint}: {exc}")

        print("[tpu-attach] 连接结果:")
        for line in results:
            print(f"  OK   {line}")
        for line in failures:
            print(f"  FAIL {line}")
        if failures:
            raise gdb.GdbError(f"部分端口连接失败: {len(failures)}/{device.core_num}")

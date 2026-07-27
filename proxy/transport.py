#!/usr/bin/env python3
import errno
import fcntl
import mmap
import os
import struct
import time

# Driver mmap ABI token; this is not a platform physical/BAR address.
DBG_FIFO_MMAP_OFFSET = 0xD00000
DBG_FIFO_MMAP_SIZE = 0x300000

# Must match driver: _IOW('D', 0x01, int)
SG_DBG_RING_DOORBELL = 0x40044401
# Optional driver barrier: _IO('D', 0x02)
SG_DBG_RING_ACQUIRE = 0x4402
_ring_acquire_supported: bool | None = None

DBG_FIFO_H2D_SIZE = 0x100000
DBG_FIFO_D2H_SIZE = 0x100000
DBG_FIFO_D2H_ASYNC_SIZE = 0x100000

DBG_FIFO_PER_CORE_SIZE = 0x20000
DBG_FIFO_RING_CTRL_SIZE = 0x8
DBG_FIFO_RING_PAYLOAD_SIZE = DBG_FIFO_PER_CORE_SIZE - DBG_FIFO_RING_CTRL_SIZE

DIR_H2D = 0
DIR_D2H = 1
DIR_D2H_ASYNC = 2

MAX_CORES = 8

MMAP_REGIONS = {
    "dbgfifo": (DBG_FIFO_MMAP_OFFSET, DBG_FIFO_MMAP_SIZE),
    # Doorbell no longer mmap'ed directly. Use ioctl SG_DBG_RING_DOORBELL on the fd.
}

# Platform-neutral 3MB mapping layout (offsets relative to mapped memory):
#   +0x000000 H2D       1MB, 8 x 128KB slots
#   +0x100000 D2H       1MB, 8 x 128KB slots
#   +0x200000 D2H_ASYNC 1MB, 8 x 128KB slots
# Doorbell delivery uses SG_DBG_RING_DOORBELL on the same device fd.

U32 = struct.Struct("<I")
_RING_CTRL = struct.Struct("<II")  # head, tail — 一次读/写 8 字节，避免轮询里重复 unpack


def _ring_used(head: int, tail: int, size: int) -> int:
    if head >= tail:
        return head - tail
    return size - (tail - head)


def _read_u32(mem: mmap.mmap, off: int) -> int:
    return U32.unpack_from(mem, off)[0]


def _write_u32(mem: mmap.mmap, off: int, val: int) -> None:
    U32.pack_into(mem, off, val)


def _read_ring_ctrl(mem: mmap.mmap, base: int) -> tuple[int, int]:
    """一次读取 head/tail（8B），供 wait 轮询使用。"""
    return _RING_CTRL.unpack_from(mem, base)


def _ring_memcpy_write(mem: mmap.mmap, data_base: int, pos: int, src: bytes) -> int:
    """按 ring 逻辑把 src 写入 payload 区（至多两段 memoryview 赋值，等同 memcpy）。"""
    n = len(src)
    if n == 0:
        return pos
    first = min(n, DBG_FIFO_RING_PAYLOAD_SIZE - pos)
    mv = memoryview(mem)
    mv[data_base + pos : data_base + pos + first] = src[:first]
    rest = n - first
    if rest:
        mv[data_base : data_base + rest] = src[first:]
    return (pos + n) % DBG_FIFO_RING_PAYLOAD_SIZE


def _ring_memcpy_read(mem: mmap.mmap, data_base: int, pos: int, length: int) -> bytes:
    """从 ring payload 区读出 length 字节（至多两次拷贝进 bytearray）。"""
    if length <= 0:
        return b""
    first = min(length, DBG_FIFO_RING_PAYLOAD_SIZE - pos)
    mv = memoryview(mem)
    if length == first:
        return bytes(mv[data_base + pos : data_base + pos + first])
    out = bytearray(length)
    out[:first] = mv[data_base + pos : data_base + pos + first]
    out[first:] = mv[data_base : data_base + (length - first)]
    return bytes(out)


def _channel_base(core_id: int, channel: int) -> int:
    if core_id < 0 or core_id >= MAX_CORES:
        raise ValueError(f"invalid core_id: {core_id}")

    if channel == DIR_H2D:
        return core_id * DBG_FIFO_PER_CORE_SIZE
    if channel == DIR_D2H:
        return DBG_FIFO_H2D_SIZE + core_id * DBG_FIFO_PER_CORE_SIZE
    if channel == DIR_D2H_ASYNC:
        return DBG_FIFO_H2D_SIZE + DBG_FIFO_D2H_SIZE + core_id * DBG_FIFO_PER_CORE_SIZE
    raise ValueError(f"invalid channel: {channel}")


def open_io(dev_path: str):
    fd = os.open(dev_path, os.O_RDWR | os.O_CLOEXEC)
    dbgfifo_offset, dbgfifo_size = MMAP_REGIONS["dbgfifo"]
    dbgfifo_vaddr = mmap.mmap(
        fd,
        dbgfifo_size,
        flags=mmap.MAP_SHARED,
        prot=mmap.PROT_READ | mmap.PROT_WRITE,
        offset=dbgfifo_offset,
    )
    # Note: no longer mapping doorbell. Ringing is done via ioctl on fd.
    return fd, dbgfifo_vaddr


def reset_h2d_ring_ctrl(dbgfifo_vaddr: mmap.mmap, core_id: int) -> None:
    base = _channel_base(core_id, DIR_H2D)
    _RING_CTRL.pack_into(dbgfifo_vaddr, base, 0, 0)


def reset_d2h_ring_ctrl(dbgfifo_vaddr: mmap.mmap, core_id: int) -> None:
    base = _channel_base(core_id, DIR_D2H)
    _RING_CTRL.pack_into(dbgfifo_vaddr, base, 0, 0)


def close_io(fd: int, dbgfifo_vaddr: mmap.mmap) -> None:
    dbgfifo_vaddr.close()
    os.close(fd)


def write_h2d(dbgfifo_vaddr: mmap.mmap, core_id: int, data: bytes, timeout_s: float = 1.0) -> None:
    if not data:
        return

    n = len(data)
    if n >= DBG_FIFO_RING_PAYLOAD_SIZE:
        raise ValueError(f"packet too large: {n}")

    base = _channel_base(core_id, DIR_H2D)
    deadline = time.monotonic() + timeout_s

    while True:
        head, tail = _read_ring_ctrl(dbgfifo_vaddr, base)
        used = _ring_used(head, tail, DBG_FIFO_RING_PAYLOAD_SIZE)
        free = DBG_FIFO_RING_PAYLOAD_SIZE - used - 1
        if free >= n:
            break
        if time.monotonic() >= deadline:  # 保护逻辑（不应走入）
            raise TimeoutError("timeout waiting h2d ring space")
        time.sleep(0.001)

    data_base = base + DBG_FIFO_RING_CTRL_SIZE
    new_head = _ring_memcpy_write(dbgfifo_vaddr, data_base, head, data)
    _write_u32(dbgfifo_vaddr, base, new_head)


def _acquire_d2h(fd: int) -> None:
    global _ring_acquire_supported
    if _ring_acquire_supported is False:
        return
    try:
        fcntl.ioctl(fd, SG_DBG_RING_ACQUIRE)
        _ring_acquire_supported = True
    except OSError as exc:
        if exc.errno != errno.ENOTTY:
            raise
        _ring_acquire_supported = False


def read_available(fd: int, dbgfifo_vaddr: mmap.mmap, core_id: int, channel: int) -> bytes:
    base = _channel_base(core_id, channel)
    head, tail = _read_ring_ctrl(dbgfifo_vaddr, base)
    used = _ring_used(head, tail, DBG_FIFO_RING_PAYLOAD_SIZE)
    if used == 0:
        return b""

    if channel != DIR_H2D:
        _acquire_d2h(fd)
    data_base = base + DBG_FIFO_RING_CTRL_SIZE
    data = _ring_memcpy_read(dbgfifo_vaddr, data_base, tail, used)
    _write_u32(dbgfifo_vaddr, base + 4, (tail + used) % DBG_FIFO_RING_PAYLOAD_SIZE)
    return data


def ring_doorbell(fd: int, core_id: int) -> None:
    """Ring the TP doorbell via the kernel ioctl."""
    if core_id < 0 or core_id >= MAX_CORES:
        raise ValueError(f"invalid core_id: {core_id}")
    fcntl.ioctl(fd, SG_DBG_RING_DOORBELL, struct.pack('i', core_id))

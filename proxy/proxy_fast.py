#!/usr/bin/env python3
"""Polling-only dbgfifo proxy (host side).

Key behaviors:
- Poll rings only while a TCP client is connected.
- Stop polling immediately after disconnect (save CPU).
- Always send `monitor exit` on disconnect for safe device-side recycle.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import threading
import time

from rcmd import filter_h2d_stream, rsp_packet, rsp_qrcmd
from transport import (
    DIR_D2H,
    DIR_D2H_ASYNC,
    close_io,
    open_io,
    read_available,
    reset_d2h_ring_ctrl,
    reset_h2d_ring_ctrl,
    ring_doorbell,
    write_h2d,
)

MAX_CORES = 8
BASE_PORT = 40090
TPU_ERR_MSG_SIZE = 4

ACCEPT_TIMEOUT_S = 0.5
IO_WAIT_S = 0.001
WRITABLE_WAIT_S = 0.002
IDLE_SLEEP_S = 0.0005
MAX_RECV_BATCH = 16
MAX_DRAIN_BATCH = 64

ERR_INTR_NAMES = (
    "ERR_TPU_INTR",
    "ERR_GDMA_ERR",
    "ERR_SDMA_ERR",
    "ERR_SORT_ERR",
    "ERR_L2M0_ERR",
    "ERR_L2M1_ERR",
    "ERR_L2M2_ERR",
)


def _warn_exit_missing_devnodes(dev_index: int) -> None:
    required = (f"/dev/tpu_dbg_event{dev_index}",)
    missing = [p for p in required if not os.path.exists(p)]
    if missing:
        for path in missing:
            print(f"WARNING: required device node not found: {path}")
        raise SystemExit(1)


def _fixed_port(dev_index: int, core_id: int) -> int:
    return BASE_PORT + dev_index * 100 + core_id


class CorePollingThread(threading.Thread):
    """One core per thread: socket<->ring bridge with D2H polling."""

    def __init__(
        self,
        core_id: int,
        host: str,
        port: int,
        manager: "ProxyFastManager",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"proxy-fast-core-{core_id}", daemon=True)
        self.core_id = core_id
        self.host = host
        self.port = port
        self.manager = manager
        self.stop_event = stop_event
        self._gdb_rx: bytearray = bytearray()
        self._async_rx: bytearray = bytearray()

    def run(self) -> None:
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind((self.host, self.port))
        listen_sock.listen(1)
        listen_sock.settimeout(ACCEPT_TIMEOUT_S)
        print(f"[proxy_fast] core={self.core_id} listen on {self.host}:{self.port}")

        try:
            while not self.stop_event.is_set():
                conn = self._accept_one(listen_sock)
                if conn is None:
                    continue
                if not self._on_connect():
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                try:
                    self._serve_client(conn)
                finally:
                    self._on_disconnect()
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            listen_sock.close()

    def _accept_one(self, listen_sock: socket.socket):
        try:
            conn, peer = listen_sock.accept()
        except socket.timeout:
            return None
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[proxy_fast] core={self.core_id} gdb connected: {peer}")
        return conn

    def _on_connect(self) -> bool:
        try:
            self.manager.acquire_io()
        except OSError as e:
            print(f"[proxy_fast] core={self.core_id} open_io failed: {e}", flush=True)
            return False
        self._gdb_rx = bytearray()
        self._async_rx = bytearray()
        assert self.manager.dbgfifo_vaddr is not None
        reset_h2d_ring_ctrl(self.manager.dbgfifo_vaddr, self.core_id)
        reset_d2h_ring_ctrl(self.manager.dbgfifo_vaddr, self.core_id)
        return True

    def _on_disconnect(self) -> None:
        try:
            self._gdb_stream_to_h2d(rsp_qrcmd(b"exit"))
        except (TimeoutError, OSError) as e:
            print(f"[proxy_fast] core={self.core_id} monitor exit failed: {e}", flush=True)
        finally:
            self.manager.release_io()

    def _gdb_stream_to_h2d(self, data: bytes) -> None:
        if not data:
            return
        assert self.manager.dbgfifo_vaddr is not None
        assert self.manager.dbg_event_fd is not None
        write_h2d(self.manager.dbgfifo_vaddr, self.core_id, data)
        ring_doorbell(self.manager.dbg_event_fd, self.core_id)

    def _send_chunk(self, conn: socket.socket, chunk: bytes) -> bool:
        view = memoryview(chunk)
        while view and not self.stop_event.is_set():
            try:
                sent = conn.send(view)
                if sent <= 0:
                    return False
                view = view[sent:]
            except (BlockingIOError, InterruptedError):
                _, writable, _ = select.select([], [conn], [], WRITABLE_WAIT_S)
                if not writable:
                    continue
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False
        return True

    def _feed_gdb_to_device(self, data: bytes, conn: socket.socket) -> None:
        out = filter_h2d_stream(
            self._gdb_rx,
            data,
            self.core_id,
            lambda r: self._send_chunk(conn, r),
        )
        if out:
            self._gdb_stream_to_h2d(out)

    def _recv_from_gdb(self, conn: socket.socket) -> bool:
        for _ in range(MAX_RECV_BATCH):
            try:
                data = conn.recv(4096)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:
                print(f"[proxy_fast] core={self.core_id} gdb disconnected (reset)")
                return False
            except OSError:
                return False

            if not data:
                print(f"[proxy_fast] core={self.core_id} gdb disconnected")
                return False

            self._feed_gdb_to_device(data, conn)
            if len(data) < 4096:
                break
        return True

    def _forward_async_error(self, conn: socket.socket, chunk: bytes) -> bool:
        self._async_rx.extend(chunk)
        parsed = 0
        while len(self._async_rx) - parsed >= TPU_ERR_MSG_SIZE:
            intr_type = int.from_bytes(
                self._async_rx[parsed : parsed + TPU_ERR_MSG_SIZE], "little"
            )
            parsed += TPU_ERR_MSG_SIZE
            err_name = (
                ERR_INTR_NAMES[intr_type]
                if 0 <= intr_type < len(ERR_INTR_NAMES)
                else f"ERR_{intr_type}"
            )
            text = f"TPU-ERR core={self.core_id} src={err_name}\n"
            if not self._send_chunk(conn, rsp_packet("O" + text.encode("utf-8").hex())):
                return False
        if parsed:
            del self._async_rx[:parsed]
        return True

    def _drain_device_rings(self, conn: socket.socket) -> bool | None:
        progressed = False

        for _ in range(MAX_DRAIN_BATCH):
            assert self.manager.dbgfifo_vaddr is not None
            chunk = read_available(
                self.manager.dbg_event_fd,
                self.manager.dbgfifo_vaddr,
                self.core_id,
                DIR_D2H,
            )
            if not chunk:
                break
            if not self._send_chunk(conn, chunk):
                return None
            progressed = True

        for _ in range(MAX_DRAIN_BATCH):
            assert self.manager.dbgfifo_vaddr is not None
            chunk = read_available(
                self.manager.dbg_event_fd,
                self.manager.dbgfifo_vaddr,
                self.core_id,
                DIR_D2H_ASYNC,
            )
            if not chunk:
                break
            if not self._forward_async_error(conn, chunk):
                return None
            progressed = True

        return progressed

    def _serve_client(self, conn: socket.socket) -> None:
        while not self.stop_event.is_set():
            progressed = False

            drained = self._drain_device_rings(conn)
            if drained is None:
                return
            progressed |= drained

            timeout = 0.0 if progressed else IO_WAIT_S
            readable, _, _ = select.select([conn], [], [], timeout)
            if readable:
                if not self._recv_from_gdb(conn):
                    return
                progressed = True

            if not progressed:
                time.sleep(IDLE_SLEEP_S)


class ProxyFastManager:
    def __init__(self, host: str, num_core: int, dev_index: int) -> None:
        self.host = host
        self.num_core = num_core
        self.dev_index = dev_index
        self.stop_event = threading.Event()
        self.services: list[CorePollingThread] = []
        self._io_lock = threading.Lock()
        self._active_conn = 0
        self._fd = None
        self.dbgfifo_vaddr = None
        self.dbg_event_fd = None

    def acquire_io(self) -> None:
        with self._io_lock:
            if self._active_conn == 0:
                dev_path = f"/dev/tpu_dbg_event{self.dev_index}"
                fd, dbgfifo_vaddr = open_io(dev_path)
                self._fd = fd
                self.dbgfifo_vaddr = dbgfifo_vaddr
                self.dbg_event_fd = fd
                print(f"[proxy_fast] io opened (dev-index={self.dev_index})", flush=True)
            self._active_conn += 1

    def release_io(self) -> None:
        with self._io_lock:
            if self._active_conn <= 0:
                return
            self._active_conn -= 1
            if self._active_conn == 0 and self._fd is not None:
                close_io(self._fd, self.dbgfifo_vaddr)
                self._fd = None
                self.dbgfifo_vaddr = None
                self.dbg_event_fd = None
                print(f"[proxy_fast] io closed (dev-index={self.dev_index})", flush=True)

    def force_close_io(self) -> None:
        with self._io_lock:
            self._active_conn = 0
            if self._fd is None:
                return
            close_io(self._fd, self.dbgfifo_vaddr)
            self._fd = None
            self.dbgfifo_vaddr = None
            self.dbg_event_fd = None
            print(f"[proxy_fast] io force-closed (dev-index={self.dev_index})", flush=True)

    def run(self) -> None:
        def _request_stop(_signum: int, _frame: object) -> None:
            self.stop_event.set()

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

        self.services = [
            CorePollingThread(
                core_id=i,
                host=self.host,
                port=_fixed_port(self.dev_index, i),
                manager=self,
                stop_event=self.stop_event,
            )
            for i in range(self.num_core)
        ]

        try:
            for t in self.services:
                t.start()
            while not self.stop_event.is_set():
                for t in self.services:
                    t.join(timeout=0.5)
        finally:
            self.stop_event.set()
            print("[proxy_fast] stopping...", flush=True)
            for t in self.services:
                t.join(timeout=2.0)
            self.force_close_io()


def main() -> None:
    p = argparse.ArgumentParser(description="polling dbgfifo proxy")
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--num-core", type=int, default=8)
    p.add_argument("--dev-index", type=int, default=0)
    args = p.parse_args()

    if args.num_core <= 0 or args.num_core > MAX_CORES:
        raise ValueError(f"num-core must be 1..{MAX_CORES}")
    if args.dev_index < 0:
        raise ValueError("dev-index must be >= 0")
    highest_port = _fixed_port(args.dev_index, args.num_core - 1)
    if highest_port > 65535:
        raise ValueError(f"fixed port out of range: {highest_port}")

    _warn_exit_missing_devnodes(args.dev_index)
    mgr = ProxyFastManager(
        host=args.listen_host,
        num_core=args.num_core,
        dev_index=args.dev_index,
    )
    mgr.run()


if __name__ == "__main__":
    main()

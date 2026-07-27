#!/usr/bin/env python3
import argparse
import mmap
import os
import queue
import sys
import select
import signal
import socket
import threading
import time
from typing import Dict

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

EVENT_STRUCT_V2_SIZE = 16
CH_D2H = 1
CH_D2H_ASYNC = 2
MAX_CORES = 8
TPU_ERR_MSG_SIZE = 4
BASE_PORT = 40090
CLIENT_IO_WAIT_S = 0.001
CLIENT_WRITABLE_WAIT_S = 0.002
EVENT_DISPATCH_WAIT_S = 0.02
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
    """启动前检查 ProxyManager 依赖的设备节点是否存在。"""
    required = (
        f"/dev/tpu_dbg_event{dev_index}",
    )
    missing = [p for p in required if not os.path.exists(p)]
    if missing:
        for path in missing:
            print(f"WARNING: required device node not found: {path}", file=sys.stderr)
        sys.exit(1)


def _fixed_port(dev_index: int, core_id: int) -> int:
    return BASE_PORT + dev_index * 100 + core_id


def _channel_to_ring_dir(channel:int):
    if channel == CH_D2H:
        return DIR_D2H
    if channel == CH_D2H_ASYNC:
        return DIR_D2H_ASYNC
    return None


class GdbServiceThread(threading.Thread):
    """每个 core 一个线程：处理 gdb->fifo 和 事件->gdb。"""

    def __init__(
        self,
        core_id: int,
        host: str,
        port: int,
        dbgfifo_vaddr,
        dbg_event_fd: int,   # used for ioctl ringing (sliding ATU)
        event_queue: "queue.Queue[int]",
        stop_event: threading.Event,
    ):
        super().__init__(name=f"gdb-core-{core_id}", daemon=True)
        self.core_id = core_id
        self.host = host
        self.port = port
        self.dbgfifo_vaddr = dbgfifo_vaddr
        self.dbg_event_fd = dbg_event_fd
        self.event_queue = event_queue
        self.stop_event = stop_event
        self._gdb_rx: bytearray = bytearray()
        self._async_rx: bytearray = bytearray()

    def run(self) -> None:
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind((self.host, self.port))
        listen_sock.listen(1)
        listen_sock.settimeout(0.5)

        print(f"[proxy] core={self.core_id} listen on {self.host}:{self.port}")

        try:
            while not self.stop_event.is_set():
                conn = self._accept_one(listen_sock)
                if conn is None:
                    continue
                try:
                    self.on_gdb_connect(conn, self.dbgfifo_vaddr)
                    self._serve_client(conn, self.dbgfifo_vaddr)
                finally:
                    self.on_gdb_disconnect(conn, self.dbgfifo_vaddr)
                    conn.close()
        finally:
            listen_sock.close()

    def _accept_one(self, listen_sock: socket.socket):
        try:
            conn, peer = listen_sock.accept()
        except socket.timeout:
            return None
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[proxy] core={self.core_id} gdb connected: {peer}")
        return conn

    def on_gdb_connect(self, conn: socket.socket, dbgfifo_vaddr) -> None:
        self._gdb_rx = bytearray()
        self._async_rx = bytearray()
        reset_h2d_ring_ctrl(dbgfifo_vaddr, self.core_id)
        reset_d2h_ring_ctrl(dbgfifo_vaddr, self.core_id)

    def on_gdb_disconnect(self, conn: socket.socket, dbgfifo_vaddr) -> None:
        # 通道断开：先 Ctrl-C 停核，再 monitor exit（杀 gdbserver 并重建）。
        try:
            self._gdb_stream_to_h2d(rsp_qrcmd(b"exit"), dbgfifo_vaddr)
        except (TimeoutError, OSError) as e:
            print(
                f"[proxy] core={self.core_id} stop+exit on disconnect: {e}",
                flush=True,
            )

    def _gdb_stream_to_h2d(self, data: bytes, dbgfifo_vaddr) -> None:
        """ 整段原样写入 H2D（包括 0x03 ）"""
        if not data:
            return
        write_h2d(dbgfifo_vaddr, self.core_id, data)
        ring_doorbell(self.dbg_event_fd, self.core_id)

    def _feed_gdb_to_device(
        self, data: bytes, conn: socket.socket, dbgfifo_vaddr
    ) -> None:
        out = filter_h2d_stream(
            self._gdb_rx,
            data,
            self.core_id,
            lambda r: self._send_chunk(conn, r),
        )
        if out:
            self._gdb_stream_to_h2d(out, dbgfifo_vaddr)

    def _recv_from_gdb(self, conn: socket.socket, dbgfifo_vaddr) -> bool:
        """一次尽量多读，减少 select/recv 往返。"""
        for _ in range(MAX_RECV_BATCH):
            try:
                data = conn.recv(4096)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:
                print(f"[proxy] core={self.core_id} gdb disconnected (reset)")
                return False

            if not data:
                print(f"[proxy] core={self.core_id} gdb disconnected")
                return False

            self._feed_gdb_to_device(data, conn, dbgfifo_vaddr)
            if len(data) < 4096:
                break

        return True

    def _drain_outbound_once(self, conn: socket.socket, dbgfifo_vaddr) -> bool:
        progressed = False
        progressed |= self._drain_device_events(conn, dbgfifo_vaddr)
        progressed |= self._drain_device_rings(conn, dbgfifo_vaddr)
        return progressed

    def _serve_client(self, conn: socket.socket, dbgfifo_vaddr) -> None:
        while not self.stop_event.is_set():
            progressed = False

            # 优先清空设备->GDB，减少额外调度延迟。
            for _ in range(MAX_DRAIN_BATCH):
                if not self._drain_outbound_once(conn, dbgfifo_vaddr):
                    break
                progressed = True

            timeout = 0.0 if progressed else CLIENT_IO_WAIT_S
            readable, _, _ = select.select([conn], [], [], timeout)
            if readable:
                if not self._recv_from_gdb(conn, dbgfifo_vaddr):
                    return
                progressed = True

            if not progressed:
                time.sleep(IDLE_SLEEP_S)

    def _send_chunk(self, conn: socket.socket, chunk: bytes) -> None:
        """ 采用 non-blocking 的方式转发数据到 host 端  """
        view = memoryview(chunk)
        while view and not self.stop_event.is_set():
            try:
                sent = conn.send(view)
                if sent <= 0:
                    raise ConnectionError("socket send returned 0")
                view = view[sent:]
            except (BlockingIOError, InterruptedError):
                _, writable, _ = select.select([], [conn], [], CLIENT_WRITABLE_WAIT_S)
                if not writable:
                    continue

    def _drain_device_events(self, conn: socket.socket, dbgfifo_vaddr) -> bool:
        progressed = False
        while True:
            try:
                channel = self.event_queue.get_nowait()
            except queue.Empty:
                return progressed

            ring_dir = _channel_to_ring_dir(channel)
            if ring_dir is None:
                continue # 报错

            chunk = read_available(
                self.dbg_event_fd, dbgfifo_vaddr, self.core_id, ring_dir
            )
            if chunk:
                if ring_dir == DIR_D2H_ASYNC:
                    self._forward_async_error(conn, chunk)
                else:
                    self._send_chunk(conn, chunk)
                progressed = True

    def _forward_async_error(self, conn: socket.socket, chunk: bytes) -> None:
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
            self._send_chunk(conn, rsp_packet("O" + text.encode("utf-8").hex()))
        if parsed:
            del self._async_rx[:parsed]

    def _drain_device_rings(self, conn: socket.socket, dbgfifo_vaddr) -> bool:
        progressed = False
        while True:
            chunk = read_available(
                self.dbg_event_fd, dbgfifo_vaddr, self.core_id, DIR_D2H
            )
            if not chunk:
                break
            self._send_chunk(conn, chunk)
            progressed = True
        return progressed


class EventDispatchThread(threading.Thread):
    """读设备事件并按 tpu 事件头中的 core 索引投递到队列。"""

    def __init__(self, event_fd: int, per_core_queue: Dict[int, "queue.Queue[int]"], stop_event: threading.Event):
        super().__init__(name="event-dispatch", daemon=True)
        self.event_fd = event_fd
        self.per_core_queue = per_core_queue
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            readable, _, _ = select.select([self.event_fd], [], [], EVENT_DISPATCH_WAIT_S)
            if not readable:
                continue
            while True:
                try:
                    raw = os.read(self.event_fd, EVENT_STRUCT_V2_SIZE)
                except (BlockingIOError, InterruptedError):
                    break
                if len(raw) != EVENT_STRUCT_V2_SIZE:
                    break
                core_id = int.from_bytes(raw[0:4], "little")
                channel = int.from_bytes(raw[4:8], "little")
                q = self.per_core_queue.get(core_id)
                if q is not None:
                    q.put(channel)


class ProxyManager:
    def __init__(self, host: str, num_core: int, dev_index: int):
        self.host = host
        self.num_core = num_core
        self.dev_index = dev_index
        self.stop_event = threading.Event()
        self.per_core_queue: Dict[int, queue.Queue[int]] = {i: queue.Queue() for i in range(num_core)}
        self.dispatcher = None
        self.services: list[GdbServiceThread] = []

    def run(self) -> None:
        # 注册停止事件信号
        def _request_stop(_signum: int, _frame: object) -> None:
            self.stop_event.set()

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

        dev_path = f"/dev/tpu_dbg_event{self.dev_index}"
        fd, dbgfifo_vaddr = open_io(dev_path)
        os.set_blocking(fd, False)
        self.dispatcher = EventDispatchThread(
            event_fd=fd,
            per_core_queue=self.per_core_queue,
            stop_event=self.stop_event,
        )
        self.services = [
            GdbServiceThread(
                core_id=i,
                host=self.host,
                port=_fixed_port(self.dev_index, i),
                dbgfifo_vaddr=dbgfifo_vaddr,
                dbg_event_fd=fd,
                event_queue=self.per_core_queue[i],  # 事件队列
                stop_event=self.stop_event,  # 事件信号
            )
            for i in range(self.num_core)
        ]

        try:
            self.dispatcher.start()
            for t in self.services:
                t.start()
            while not self.stop_event.is_set():
                for t in self.services:
                    t.join(timeout=0.5)
        finally:
            self.stop_event.set()
            print("[proxy] stopping...", flush=True)
            if self.dispatcher is not None:
                self.dispatcher.join(timeout=2.0)
            for t in self.services:
                t.join(timeout=2.0)
            close_io(fd, dbgfifo_vaddr)


def main() -> None:
    p = argparse.ArgumentParser(description="gdb proxy for 2260(e)")
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

    mgr = ProxyManager(
        host=args.listen_host,
        num_core=args.num_core,
        dev_index=args.dev_index,
    )
    mgr.run()


if __name__ == "__main__":
    main()

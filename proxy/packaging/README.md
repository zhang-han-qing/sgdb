# gdb-proxy 与 `sgcard` 驱动生命周期联动

本目录提供把 `proxy.py` 与 `sgcard` 模块装载/卸载流程绑定的配置（**按实际设备数量自动拉起实例**）：

- `linux/systemd/gdb-proxy@.service` — 按 `%i`（dev-index）启停 `proxy.py`。
- `linux/default/gdb-proxy` — 全局默认参数（所有 `gdb-proxy@N` 共用）。
- `linux/modprobe.d/sgcard-gdb-proxy.conf` — `modprobe` 时扫描 `/dev/sg-host-drv-*` 并启动全部实例。
- `linux/udev/99-gdb-proxy-sgcard.rules` — 新设备节点出现时触发 `gdb-proxy-refresh.service` 兜底刷新。
- `linux/systemd/gdb-proxy-refresh.service` — 调用脚本刷新实例列表。
- `../scripts/gdb_proxy_instances.sh` — 扫描设备并批量 start/stop `gdb-proxy@N`。
- `install_sc11.sh` / `uninstall_sc11.sh` — 安装与卸载。

## 1. 这套方案解决什么

每个 `proxy.py` 进程绑定一个 `dev-index`，长期持有 `/dev/sg-host-drv-{N}` 与 `/dev/tpu_dbg_event{N}`。实例数量由当前设备节点自动决定。

`modprobe`/`modprobe -r` 时钩子会统一 start/stop 全部实例；`udev` 在节点晚到时会触发一次 refresh。

## 2. 工作流（最少操作）

```
sudo modprobe sgcard      # → 自动扫描并启动 gdb-proxy@N
sudo modprobe -r sgcard   # → stop 全部实例 → 卸模块
```

## 3. 安装

```bash
cd path/to/gdb_proxy
sudo sh packaging/install_sc11.sh              # 默认 1690 (8 core)
sudo sh packaging/install_sc11.sh 1690e        # 1690e (4 core)
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
```

一般不必 `systemctl enable`；只跟模块走即可。

### 自定义安装前缀 / 暂存目录

```bash
sudo env PREFIX=/opt/myorg/gdb-proxy sh packaging/install_sc11.sh
sudo env DESTROOT=/tmp/stage sh packaging/install_sc11.sh   # 打包用
sudo sh packaging/uninstall_sc11.sh   # 本机卸载（DESTROOT/PREFIX 与安装时一致）
```

更换 `PREFIX` 后，同步修改 `gdb-proxy@.service` 中 `WorkingDirectory=` 与 `ExecStart=`，再 `daemon-reload`。

## 4. 配置项

`/etc/default/gdb-proxy` 变量：

| 变量 | 含义 |
|------|------|
| `GDB_PROXY_LISTEN_HOST` | 监听地址 |
| `GDB_PROXY_NUM_CORE` | core 数（≤ 8） |

`GDB_PROXY_SCRIPT_DIR` 可在 unit 里追加 `Environment=`，或由 `rcmd.py` 默认使用 `PREFIX/scripts/`。

修改后：

```bash
sudo /usr/lib/tpu-gdb-proxy/scripts/gdb_proxy_instances.sh restart
```

## 5. script tpu_hang_info（host 侧执行）

目标机需有 `<gdb_proxy>/scripts/tpu_hang_info_host.py`。GDB 侧：

```gdb
monitor script
monitor script tpu_hang_info
```

## 6. 调试与日志

```bash
sudo systemctl status 'gdb-proxy@*'
sudo /usr/lib/tpu-gdb-proxy/scripts/gdb_proxy_instances.sh refresh
journalctl -u 'gdb-proxy@*' -e
```

## 7. 注意事项

- **务必使用 `modprobe` / `modprobe -r`**，勿裸 `rmmod`。
- **`modprobe` 路径**：钩子里为 `/sbin/modprobe`，部分系统需改为 `/usr/sbin/modprobe`。
- **端口规则**：按 `50090 + dev-index * 100 + core-id` 固定计算。
- **网络**：默认 `0.0.0.0`；生产建议 `127.0.0.1` 或防火墙。
- **其它 fd 持有者**：stop proxy 后若仍无法 `rmmod`，检查是否有其它进程占用同一设备节点。

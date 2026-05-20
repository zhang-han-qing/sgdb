# gdb-proxy scripts

将 `tpu_hang_info_host.py` 放在此目录（默认会从 `rcmd.py` 同级 `scripts/` 查找）。

如果需要自定义路径，可设置环境变量 `GDB_PROXY_SCRIPT_DIR`。

GDB 命令：

```gdb
monitor script tpu_hang_info
monitor script tpu_hang_info 0
```

或通过 sgdb：

```gdb
script tpu_hang_info
script tpu_hang_info 0
```

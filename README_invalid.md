# sgdb

轻量 GDB Python 扩展，当前提供命令：
- `sgdb`（显示已注册命令）
- `tpu-attach <device-type> <device-id>`
- `tp <idx>`（按 tp 编号切换 inferior）
- `rx /<n><format><units> +addr`（读取物理地址）
- `rset *(int|float*)addr = value`（写入物理地址，4字节）
- `script <name> [args...]`（host 侧脚本入口，当前支持 `tpu_hang_info`）

## 项目结构

- `sgdb/plugins/`：gdb-host 扩展（命令、设备定义、状态管理）
- `sgdb/proxy/`：gdb_proxy 守护与安装脚本
- `sgdb/gdbserver2/`：TP 侧 gdbserver2 源码与构建产物

### plugins 子目录

- `sgdb/plugins/devices/`：设备特性定义与注册表
- `sgdb/plugins/commands/`：扩展命令实现
- `sgdb/plugins/gdbinit.py`：路径修正 + 命令注册（供 `~/.gdbinit` `source`）
- `sgdb/plugins/install.sh`：一次性安装自动加载配置

## 一次性安装（之后无需每次手动 source）

```bash
bash /path/to/sgdb/plugins/install.sh
```

该脚本会根据 `install.sh` 的实际位置自动推导 `gdbinit.py` 路径，并向 `~/.gdbinit` 追加：

```text
source /your/local/path/to/sgdb/plugins/gdbinit.py
```

之后每次启动 GDB 都会自动加载 `sgdb`。

可选环境变量：
- `GDBINIT_FILE`：指定目标 gdbinit 文件（默认 `~/.gdbinit`）
- `SGDB_GDBINIT_PATH`：覆盖默认 loader 路径（默认自动使用脚本目录下 `gdbinit.py`）

## 当前支持设备类型

- `1690`（8 core）
- `1690e`（4 core）

## 使用

启动 GDB 后直接执行：

```gdb
help sgdb
sgdb
help tpu-attach
tpu-attach 1690 0
tp 3
rx /4xw 0x6908010000
rx 0x6908010000
rset *(int*)0x6908010000 = 0x1
script tpu_hang_info
script tpu_hang_info 0
```

`script` 命令会先向 `gdb_proxy` 发送 `monitor script` 做能力协商，再执行对应脚本。

`rx` 参数说明（对齐 `x` 使用习惯）：
- 语法：`rx [/<n><format><units>] addr`
- `n`：读取元素个数（默认 `1`）
- `format`：`x/u/d/f`
- `units`：`b/h/w/g` 对应 `1/2/4/8` 字节

`rset` 首版仅支持 `int` 与 `float`：
- `rset *(int*)0x6908010000 = 0x1234`
- `rset *(float*)0x6908010000 = 1.5`

`tpu-attach` 前置条件（简化版流程）：
- 启动 GDB 后，`info inferiors` 必须仅有 `inferior 1`。
- 若当前不是初始态（例如已有多个 inferior），命令会 warning 并直接退出。
- 命令内部会统一设置：
  - `set mi-async on`（不支持时会提示并跳过）
  - `set non-stop on`
  - `set schedule-multiple on`
- 每次会提示：`[tpu-attach] update sysroot from remote? (y/N):`

命令会提示输入 target ip（默认记忆上次输入），端口固定公式：
- `40090 + device-id * 100 + tp-sys-id`

## Sysroot 缓存

- 缓存目录：`<repo>/.sgdb_cache/sysroots/<device-name>/`
- 远端源路径：`/lib/firmware/tpuv7/tp_rootfs.cpio`（可以是软链接）
- 当选择 `update`（输入 `y/yes`）时：
  - 仅执行一次 `scp` 拉取到本地临时文件（通常只需一次密码输入）
  - 本地计算 md5，缓存到 `<md5>/rootfs`
  - 若该 md5 已存在，则直接复用
  - `current` 软链接会指向最新使用的 md5 目录
- 当选择 `N`（默认）时：
  - 不访问远端，直接复用本地缓存
  - 若本地无缓存，则跳过 sysroot 设置（调试仍可继续）
- 依赖工具：`ssh/scp` + `bsdtar`（推荐）或 `cpio`

最小验证步骤：
1. `gdb-multiarch`
2. `tpu-attach 1690 0`
3. `info inferiors`（应为 1..8）
4. `tp 0`

# multitui

把 N 个独立的终端会话（`gdb` / `gdb-multiarch` / `bash` ...）包进一个 Textual 界面：

- 左侧：每个**非聚焦** core 一个子窗，显示状态（RUNNING/STOP/DEAD）、实时滚动输出，以及**该 core 专属的命令输入框**（带 `core N >` 边框标题，无需切核即可发命令）。**双击**某个子窗即可把它切换为聚焦 core；
- 右侧（主区）：顶部有 `>>> core N` 标题标明当前聚焦核；下方是该 core 的**原生终端显示**，CLI / TUI（`ctrl-x ctrl-a`）均可，并可交互；
- 右侧底部：醒目的 `(global)` 输入框（高亮边框），命令广播到所有 core；
- 最底部：`Footer` 显示快捷键提示（`Ctrl+Q` 退出、`Ctrl+C` 中断聚焦核）。

本框架与 SGDB/TPU **完全解耦**，每个 core 启动什么命令由 `--cmd` 决定，默认 `bash`，可随手测试。

## 设计

- 每个 core = 一个 PTY 子进程 + 一个 pyte 屏幕（`backend.py`）。pyte 是唯一的事实来源：
  - 右侧整屏渲染读 pyte 屏幕缓冲（含颜色）；
  - 左侧滚动行由 `LineEmittingScreen` 在换行时回调产生（终端感知，正确处理 `\r`/控制码）。
- 右侧终端 widget（`terminal.py`）把 pyte 屏幕渲染成 Rich 文本，并把按键转义后写回 PTY；resize 时同步 pyte / `TIOCSWINSZ` / `SIGWINCH`。
- App（`app.py`）用 asyncio `add_reader` 读取 8 路 PTY，路由聚焦/广播。

## 安装（docker mlir2 内）

1. gdb-multiarch（需带 Python 与 RISC-V 支持）：

```bash
apt-get update && apt-get install -y gdb-multiarch
# 验证 RISC-V：
gdb-multiarch -batch -ex "set architecture riscv:rv64" -ex "show architecture"
```

2. Python 3.8+（容器内一般已具备）。

3. Python 包 pyte / textual（建议用 venv）：

```bash
cd sgdb
python3 -m venv multitui/.venv
multitui/.venv/bin/pip install pyte textual
# 无外网时用内网镜像： pip install -i <mirror-url> pyte textual
```

## 运行

**必须在 `sgdb/` 目录下执行**（该目录下同时有 `multitui/` 与 `plugins/`）。若在 docker 里挂载的是上级目录，请先：

```bash
cd /workspace/sgdb   # 或你的 sgdb 实际路径
ls multitui/__main__.py plugins/gdbinit.py   # 两个文件都应存在
```

推荐用启动脚本（自动设置 `PYTHONPATH` 与工作目录）：

```bash
cd sgdb
chmod +x run_multitui.sh
./run_multitui.sh --ip 172.24.12.100 --device 1690 --cores 8 --gdb gdb-multiarch
```

或手动：

### 1) 自测模式（不接硬件）

```bash
# 默认 4 个 bash
PYTHONPATH=. multitui/.venv/bin/python -m multitui --cores 4
# 也可用本机 gdb
PYTHONPATH=. multitui/.venv/bin/python -m multitui --cores 8 --cmd gdb
```

### 2) SGDB 接入模式（每核 attach 到 tp_daemon）

传入 `--ip` 即进入 SGDB 模式：每个核以 `gdb-multiarch` 启动，自动 `source plugins/gdbinit.py` 并执行 `tp-attach <device> <device-id> <core> <ip>`。

```bash
# 当前目录必须是 sgdb（不是 /workspace 上级）
PYTHONPATH=. multitui/.venv/bin/python -m multitui \
    --ip 172.24.12.100 --device 1690 --cores 8 --gdb gdb-multiarch
```

若 `-m multitui` 报 `No module named multitui.__main__`，说明当前目录不对或代码未同步完整；可改用：

```bash
PYTHONPATH=. multitui/.venv/bin/python multitui/__main__.py --ip ... 
```

参数：

- `--ip`：目标设备 IP（给了就进 SGDB 模式）；
- `--device`：设备类型，默认 `1690`（可 `1690e`）；
- `--device-id`：默认 `0`；
- `--cores`：核数，不传则取设备默认（1690=2，1690e=4）；
- `--gdb`：默认 `gdb-multiarch`。

端口 = `40090 + device-id*100 + core`。需保证该端口（设备/代理）可达，且 sgdb 插件目录完整。

操作：

- **双击**左侧某个 core 子窗 → 切换为聚焦 core（替代下拉框）；
- 右侧终端聚焦时，按键直接发给该 gdb（含 `ctrl-x ctrl-a` 切 TUI、`ctrl-c` 中断）；
- 左侧每个子窗有独立输入框，回车把命令发给**该** core；
- 底部 `(global)` 输入回车广播给**所有** core；输入 `core N` 也可切换聚焦；
- `ctrl+q` 退出。

## 性能

- 右侧渲染按行做 style 游程合并 + Style 缓存，刷新以 ~30Hz 帧节流（仅在有新输出时重绘），避免高频重绘卡顿。

## 已知限制 / 后续

- 状态判定用「屏幕底行是否 `(gdb)` 提示符」启发式，bash 等非 gdb 会一直显示 RUNNING。
- 失焦 core 若停在原生 TUI，左侧滚动会失去逐行语义（可后续在失焦时自动 `tui disable`）。
- 嵌入终端内的鼠标转发未做（如需在 gdb TUI 内点击，可后续把鼠标事件转义给 PTY）。

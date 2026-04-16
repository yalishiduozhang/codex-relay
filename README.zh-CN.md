<div align="center">
  <img src="assets/logo.svg" alt="codex-relay logo" width="600" />
</div>

# codex-relay

`codex-relay` 是一个零第三方运行时依赖的 Codex CLI/TUI 配置切换工具。
它同时支持 relay 型 API key 站点和官方 Codex 订阅，帮助你在不手改 `~/.codex/config.toml` 与 `~/.codex/auth.json` 的情况下安全切换。

English documentation: [README.md](README.md)

## 项目简介

很多 Codex 用户会在两种状态之间来回切换：

- relay 站点，依赖 `base_url + OPENAI_API_KEY`
- 官方 Codex 订阅，依赖 `auth_mode + tokens`

这两类配置结构并不一样，手改很容易出错。`codex-relay` 把它整理成一套可重复的工作流：

- 保存多个 relay / official profile
- 安全切换当前使用状态
- 保留备注和使用记录
- 用真实 Codex 兼容路径做测活
- 通过命令行或内置 TUI 管理全部配置

## 核心特性

- 零第三方运行时依赖
- 直接与现有 `~/.codex` 协作
- 支持两种 profile 类型：`relay` 和 `official`
- 自动迁移旧版仅 relay 的 profile store
- 切换时只更新必要字段，不破坏其它 Codex 配置
- 为 official profile 保留完整认证快照，支持稳定往返切换
- 支持从另一套 Codex 目录导入快照
- 支持通过原生 `codex login --device-auth` 创建官方 profile
- 切换前自动备份当前 live 配置
- 支持 HTTP probe 和真实 `codex exec` probe
- official profile 会自动跳过 relay 风格 HTTP probe
- 单个 profile 探测异常不会拖垮整批测试
- 内置 TUI，支持类型标签页、多选测活和可滚动结果查看

## 环境要求

- Python 3.11 或更高版本
- 如果要使用以下能力，本机需要安装 Codex CLI：
  - `login-official`
  - `--via codex`
  - TUI 内官方登录
- 如果要使用 TUI，建议在 Linux、macOS 或支持 `curses` 的环境中运行

## 安装方式

### 方式一：直接从源码运行

```bash
git clone https://github.com/yalishiduozhang/codex-relay.git
cd codex-relay
PYTHONPATH=src python -m codex_relay --help
```

### 方式二：安装成本地命令

```bash
git clone https://github.com/yalishiduozhang/codex-relay.git
cd codex-relay
python -m pip install -e .
codex-relay --help
```

## 快速开始

### 查看当前状态

```bash
codex-relay current
codex-relay list
```

### 添加 relay profile

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key sk-example \
  --note "主力站"
```

### 保存当前 live 配置

```bash
codex-relay save-current snapshot-1 --note "当前可用状态"
```

### 通过原生登录创建官方 profile

```bash
codex-relay login-official official-main
```

### 从另一套 Codex 目录导入快照

```bash
codex-relay import ~/.codex-backup/example-codex --name official-backup
```

### 激活已保存 profile

```bash
codex-relay use official-main
codex-relay use relay-a
```

### 执行测活

```bash
codex-relay probe relay-a
codex-relay probe-all
```

### 打开交互界面

```bash
codex-relay tui
```

## Profile 类型

### Relay profile

relay profile 会保存：

- `base_url`
- `api_key`
- 备注
- 测活记录

### Official profile

official profile 会保存：

- 从 `auth.json` 提取的 `auth_snapshot`
- 归一化后的 provider/config 快照
- `auth_mode`
- 可用时的官方账号摘要
- 备注
- 测活记录

## 官方登录流程

要创建新的官方 profile，`codex-relay` 可以在隔离的 `CODEX_HOME` 里调用原生 `codex login --device-auth`。

这意味着：

- 登录过程中不会污染当前 live `~/.codex`
- 浏览器登录链路仍然是原生 Codex
- 只有校验通过后才会写入 profile store

在 TUI 里，官方登录会临时退出 curses，把终端交给原生 Codex 登录，完成后再干净地回到 TUI。

## 测活逻辑

### 可用模式

- `http`
- `codex`
- `both`

### Relay profile 的测活

relay profile 可以使用两种测活方式：

- 基于 Responses 风格接口的 HTTP probe
- 在隔离运行时中执行真实 `codex exec`

### Official profile 的测活

official profile 会自动只走 `codex` probe。
它不需要 API key，也不会尝试 relay 风格的 HTTP probe。

### 鲁棒性

- 单个 profile 探测异常不会中断整批测试
- 结果会按 profile、按 method 分别保存
- `reply/detail` 会较完整地保留，便于后续查看

## TUI

启动：

```bash
codex-relay tui
```

### 主界面能力

- 顶部 `All / Relay / Official` 标签页
- 搜索和类型过滤
- 新增 relay profile
- 通过原生 Codex 登录创建 official profile
- 导入 profile 快照
- 保存当前 live 配置
- 切换、编辑、删除 profile
- 多选待测 profile
- 可滚动的详情和测活结果弹窗

### 常用快捷键

- `Enter` 或 `u`：激活当前 profile
- `a`：新增 relay profile
- `o`：通过原生 Codex 登录新增 official profile
- `I`：从目录导入 profile 快照
- `e`：编辑当前 profile
- `d`：删除当前 profile
- `s`：保存当前 live 配置
- `Space`：标记或取消标记当前 profile
- `A`：标记或反标记当前可见列表
- `C`：清空所有测试标记
- `p`：优先测试已标记 profile；如果没有标记，则测试当前项
- `P`：测试全部可见 profile
- `v`：切换 probe 模式
- `t`：循环切换类型标签页
- `Tab` 或 `Shift-Tab`：切换类型标签页
- `/`：搜索
- `c`：清空搜索过滤
- `i`：打开完整详情
- `g`：跳到当前激活项
- `h` 或 `?`：帮助
- `q`：退出

结果弹窗支持：

- `Up` 或 `Down`
- `PgUp` 或 `PgDn`
- `Home` 或 `End`

## 开发与测试

运行测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

直接从源码启动：

```bash
PYTHONPATH=src python -m codex_relay --help
```

## License

MIT

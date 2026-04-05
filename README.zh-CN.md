<div align="center">
  <img src="assets/logo.svg" alt="codex-relay logo" width="600" />
</div>

# codex-relay

`codex-relay` 是一个面向 Codex 用户的中转站管理工具。它提供零第三方运行时依赖的命令行界面与终端交互界面，帮助你安全、清晰地管理多个 Codex 兼容 relay，而不必反复手改 `~/.codex/config.toml` 和 `~/.codex/auth.json`。

English documentation: [README.md](README.md)

## 项目简介

对于经常切换多个 relay 的用户来说，手动修改 Codex 配置通常既低效又容易出错。典型流程往往是：

1. 打开 `~/.codex/config.toml`
2. 手动替换 `base_url`
3. 打开 `~/.codex/auth.json`
4. 手动替换 `OPENAI_API_KEY`
5. 试着记住之前还能用的配置

`codex-relay` 将这套机械流程整理为一套可持续维护的工作流：

- 保存多个 relay profile
- 为每个 relay 添加备注
- 安全切换当前使用的站点
- 批量测活并记录结果
- 使用命令行或 TUI 进行日常管理

## 适用场景

如果你符合下面任意一种情况，这个工具会很有帮助：

- 手里维护多个 relay 或公益站
- 经常需要在不同中转站之间切换
- 想保留每个站点的备注和使用记录
- 希望快速知道“哪个站现在能用、哪个站更稳”
- 不想因为试错而覆盖掉当前可用的 Codex 配置

## 核心特性

- 零第三方运行时依赖
- 与现有 `~/.codex` 配置直接协作
- 使用独立 profile 档案保存多个 relay
- 切换前自动备份当前 live 配置
- 只更新必要字段，不破坏其余 Codex 配置
- 支持新增、编辑、重命名、删除、查看当前配置
- 首次运行自动导入当前 live relay
- 支持两类测活：
  - 接近真实 Codex 请求的 HTTP 探针
  - 真实 `codex exec` 探针
- 默认同时执行 `http + codex`
- 内置 TUI，支持筛选、详情、测活和快速切换
- 使用文件锁保护 profile 存储，避免并发写入覆盖数据

## 环境要求

- Python 3.11 或更高版本
- 如果要使用 `--via codex`，需要本机已安装 Codex CLI
- 若使用 TUI，建议在 Linux、macOS 或支持 `curses` 的环境中运行

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

### 1. 查看当前状态

```bash
codex-relay current
codex-relay list
```

### 2. 添加一个 relay

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key <API_KEY> \
  --note "主力站"
```

### 3. 切换到该 relay

```bash
codex-relay use relay-a
```

### 4. 执行测活

```bash
codex-relay probe relay-a
```

### 5. 打开交互界面

```bash
codex-relay tui
```

## 详细使用说明

### 列出所有已保存的 profile

```bash
codex-relay list
```

输出会显示：

- 已保存的 profile 名称
- 当前激活项
- 脱敏后的 key
- 备注
- 最近一次 probe 摘要
- 最近使用时间

### 查看当前 live Codex 配置

```bash
codex-relay current
```

这个命令会显示：

- 当前 provider
- 当前模型
- 当前 live `base_url`
- 脱敏后的 API key
- 是否能匹配到某个已保存的 profile

### 添加 profile

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key <API_KEY> \
  --note "主力站"
```

如果希望添加后立刻切换：

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key <API_KEY> \
  --note "主力站" \
  --activate
```

### 保存当前 live 配置

```bash
codex-relay save-current snapshot-1 --note "从当前可用配置保存"
```

这个命令适合用于“我已经手动调好了当前配置，现在想把它收编进 profile 库”。

### 切换到某个已保存的 profile

```bash
codex-relay use relay-a
```

也可以按索引切换：

```bash
codex-relay use --index 2
```

切换时，`codex-relay` 会：

1. 先备份当前 `config.toml`
2. 先备份当前 `auth.json`
3. 只修改当前 provider 的 `base_url`
4. 只修改 `OPENAI_API_KEY`
5. 保留其它 Codex 配置不变

### 编辑 profile

重命名：

```bash
codex-relay edit relay-a --rename relay-main
```

修改 URL：

```bash
codex-relay edit relay-a --url https://new-relay.example.com
```

修改 API key：

```bash
codex-relay edit relay-a --key <NEW_API_KEY>
```

修改备注：

```bash
codex-relay edit relay-a --note "工作日晚间更稳定"
```

如果被编辑的 profile 正处于激活状态，且你修改了 URL 或 key，工具会同步更新 live Codex 配置。

### 删除 profile

```bash
codex-relay remove relay-a
```

这会删除保存的档案项；如果当前 live 配置仍然指向它，工具会给出提醒，但不会擅自改写你当前的 live 配置。

## 测活说明

### 测试单个 relay

```bash
codex-relay probe relay-a
```

默认会同时执行两种测活：

- HTTP 探针
- Codex 探针

### 测试全部 relay

```bash
codex-relay probe-all
```

### 只跑 HTTP 探针

```bash
codex-relay probe relay-a --via http
```

### 只跑真实 Codex 探针

```bash
codex-relay probe relay-a --via codex
```

### 使用自定义消息测活

```bash
codex-relay probe relay-a --message "你好，你是谁？"
```

### 使用期望文本做功能性校验

```bash
codex-relay probe relay-a \
  --message "Reply with exactly 42" \
  --expect 42
```

这类校验更适合做“功能性测活”，而不是只检查接口是否联通。

### Probe 输出包含哪些信息

对于每个 profile、每种 probe 方法，工具都会尽量展示：

- 是否成功
- 返回状态码
- 延迟
- 模型回复
- 失败时的错误细节

`both` 模式的意义在于：

- `http` 更轻量，适合快速协议级检查
- `codex` 更接近真实使用场景

## TUI 使用说明

启动方式：

```bash
codex-relay tui
```

TUI 主界面会显示：

- 当前激活 profile
- 左侧 profile 列表
- 右侧详情与最近回复
- 当前 probe 配置
- 底部状态提示

### TUI 常用按键

- `h` 或 `?`：打开帮助
- `Enter` 或 `u`：切换到当前选中项
- `a`：新增 profile
- `e`：编辑当前选中 profile
- `d`：删除当前选中 profile
- `s`：把当前 live 配置保存成 profile
- `p`：测试当前选中 profile
- `P`：测试所有当前可见项
- `v`：切换 probe 模式 `both / http / codex`
- `m`：修改 probe message
- `x`：修改 expected substring
- `/`：按名称、URL、备注搜索
- `c`：清空当前过滤条件
- `i`：打开完整详情弹窗
- `g`：跳转到当前激活项
- `PgUp` 或 `PgDn`：在长列表中快速移动
- `Home` 或 `End`：跳到首个或最后一个可见项
- `q`：退出

## 实现原理

### Profile 存储

默认保存在：

```text
~/.codex/relay_profiles.json
```

每个 profile 包含：

- `name`
- `base_url`
- `api_key`
- `note`
- `created_at`
- `updated_at`
- `last_used_at`
- `last_probe`

### HTTP probe 的实现方式

HTTP 探针会：

- 优先尝试 `.../responses`
- 必要时回退到 `.../v1/responses`
- 构造接近真实 Codex 的 `responses` 请求体
- 解析 SSE 流式输出
- 提取最终模型回复

### Codex probe 的实现方式

Codex 探针会：

- 创建隔离的临时 `CODEX_HOME`
- 注入选中 relay 的 URL 与 key
- 调用真实 `codex exec`
- 读取输出文件中的最终回复

## 开发与测试

运行测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

直接从源码运行：

```bash
PYTHONPATH=src python -m codex_relay --help
```

## 项目结构

```text
codex-relay/
├── .github/workflows/ci.yml
├── LICENSE
├── README.md
├── README.zh-CN.md
├── pyproject.toml
├── src/codex_relay/
│   ├── __init__.py
│   ├── __main__.py
│   └── cli.py
└── tests/
    ├── helpers.py
    ├── test_cli_workflows.py
    ├── test_probe_http.py
    └── test_tui_and_hygiene.py
```

## License

MIT

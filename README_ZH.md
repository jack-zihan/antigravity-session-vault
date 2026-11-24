# Antigravity Session Vault（会话保险库）

> **让 Antigravity Desktop 的主对话永不丢失，同时把历史 Swarm 与子会话自动归档到可检索冷库，彻底告别 500 上限误删与幽灵卡片。**

[English README](./README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![零外部依赖](https://img.shields.io/badge/%E4%BE%9D%E8%B5%96-%E9%9B%B6%E5%A4%96%E9%83%A8%E4%BE%9D%E8%B5%96%20(stdlib)-22C55E.svg)](#)
[![跨平台](https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-Windows%20%7C%20macOS%20%7C%20Linux-64748B.svg)](#)

---

## 为什么需要这个插件？

Antigravity Desktop 每次启动时都会对 `~/.gemini/antigravity/conversations/` 执行写死的 `PruneAll(500)` 清理。问题在于，每一个 Swarm Worker 与 Subagent 子会话都会与你的主对话**共享这 500 个平坦配额**：

1. 重度使用 Swarm / 子代理后，几百个临时子会话迅速撑爆 500 上限；
2. 下次重启时，`language_server` 按 `.db` 文件修改时间（`mtime`）直接删除最旧的文件——经常把你**宝贵的历史主对话**连同 `brain/<id>/` 一起误删；
3. 由于 `PruneAll(500)` 不会同步清理 `agyhub_summaries_proto.pb` 与 `conversation_summaries.db`，被删掉的旧对话会在侧边栏变成点开后**永远转圈的幽灵卡片**。

**Antigravity Session Vault** 以 **原生 AGY 插件** 形态彻底解决该问题：不修改官方二进制、零第三方依赖、且平时不开 AGY 时**零后台常驻进程**。

---

## 工作原理

### 随 AGY 生灭的零负担生命周期

1. **原生 Hook 按需唤醒（`hooks.json`）**：在 `SessionStart` / `PreInvocation` 触发时，毫秒级探测内核单例锁（运行中时 <2ms 直接返回 `{}`）；若未启动，则拉起一个完全脱离父进程树与 Windows Job Object 的守护进程（`watch --exit-after-clean`）。
2. **AGY 运行期间**：
   - **第一层 · 在线 `mtime` 护盾（每 2 分钟）**：仅将已完成/空闲子会话的 `.db` 文件 `mtime` 降权到 `2010`/`2015`（UI 侧边栏按 Protobuf 内 `lastModifiedTime` 排序，视觉完全无感），确保就算突发断电或崩溃重启，也绝对不会先误删主对话。
   - **第三层 · WAL 安全增量保险库（每 30 分钟）**：使用 SQLite 官方在线 `backup()` API + `PRAGMA quick_check`，将所有主对话、Side Question、标注及 `brain/` 产物增量快照到保险库目录。
3. **AGY 关闭瞬间**：
   - **第二层 · 离线智能归档与双库自愈（<1.5 秒）**：通过 fail-closed 进程与文件锁门禁（自动忽略内联崩溃/自重启），将符合条件的旧子会话无损迁移至 `<archive_dir>/subagents/<cid>/`，自动维护可检索索引 `index.jsonl`，并同步清理 SQLite（含 `conversations_fts` 完整性校验）与 `agyhub_summaries_proto.pb` 中的已归档项和历史幽灵项（完整保留 CLI 记录与未知字段）。
   - **干完活自动退出**：离线归档完成后守护进程立即自行结束，平时电脑里 **0 进程残留**。

### 简洁的 4 层架构

插件采用零外部依赖的 4 层单向依赖结构（`session_vault/` + `scripts/hook.py`），模块职责单一、无循环依赖：

- **Layer 0（`config.py`、`proto.py`、`platform_ops.py`）**：严格配置校验与日志构建、字节级无损 Protobuf 编解码、按目录隔离的内核单例锁、抗 Job Object 的脱离拉起。
- **Layer 1（`store.py`）**：原子写盘、`index.jsonl` 索引读写、防冲突元文件滚动快照、8 类会话分类器、`PruneAll(500)` 风险模拟与离线安全门禁。
- **Layer 2（`shield.py`、`archive.py`、`vault.py`）**：三层留存逻辑彼此解耦——在线 `mtime` 护盾、离线四级智能归档与双库清理、增量 WAL 安全主对话备份、归档检索与离线还原。
- **Layer 3（`watcher.py`、`cli.py`、`scripts/hook.py`）**：事件驱动生命周期守护进程（拿锁后再按需加载重模块）、只读命令零日志副作用、快路径 Hook 入口（恒定返回 `{}`）。

### 重要度优先 + 时间兜底与对话式审核

时间长短不等于重要程度：5 天前的核心科研 Teamwork 过程往往比 2 小时前的一次性 Swarm 更有价值。插件支持在对话里直接用 `trees` + `policy` 按主对话重要性标注留存优先级：

| 优先级 | 范围 | 处理方式 |
|---|---|---|
| **Priority 0（永不归档）** | 全部主对话、Side Question（`conversation_parents` + `annotations/*.pbtxt`）、置顶/标注对话、未建索引新对话、运行中任务、近 5 分钟内活跃会话，以及 `keep_subagents` 点名锁定的子会话（`restore` 还原的子会话会自动加入） | 始终留在原位；主对话与 Side Question 持续增量快照至保险库 |
| **Tier 1（显式优先归档）** | 标记在 `archive_roots`（`policy archive <id>`）名下的已结束/空闲子会话 | 退出时最先无损归档，不受 2 天/3 天限制，立刻腾出配额 |
| **Tier 2（默认时间归档）** | `auto` 组中超过 **2 天** 未活跃主对话名下的旧子会话 | 退出时按时间从旧到新无损归档 |
| **Tier 3（安全阀 · `auto` 组）** | 仅当活跃总数仍高于 **`target_max = 420`** 时触发 | 优先从 `auto` 组归档最早的已关闭（`killed == 1`）子会话，其次是最早的空闲子会话 |
| **Tier 4（受保护的 `keep_roots`）** | 标记在 `keep_roots`（`policy keep <id>`）的重要主对话整棵子会话树 | 无视 2 天过期限制优先留在原位；只有连 `keep_roots` 自身加起来都超 `420` 时，才按最旧优先归档其最早的死子会话 |

被归档的子会话**无需重启 AGY 即可随时在线检索与读取**；需要恢复回侧边栏时离线执行 `restore <uuid>`（自动加入 `keep_subagents` 避免下次退出又被踢出）；确认彻底不再需要的冷归档可显式执行 `discard --yes` 释放空间。

---

## 安装方式

### 环境要求
- **Antigravity Desktop 2.0+**
- **Python 3.10+**（纯标准库，无需安装任何第三方包）

### 快速安装

将本仓库克隆或复制到 Antigravity 原生插件目录即可自动发现并启用：

```powershell
# Windows (PowerShell)
git clone https://github.com/jack-zihan/antigravity-session-vault.git `
  "$env:USERPROFILE\.gemini\config\plugins\antigravity-session-vault"
```

```bash
# macOS / Linux
git clone https://github.com/jack-zihan/antigravity-session-vault.git \
  ~/.gemini/config/plugins/antigravity-session-vault
```

使用 AGY CLI 验证加载状态（或在 Antigravity Desktop 设置 → Plugins 中查看 **Session Vault**）：

```bash
agy plugin list
```

在插件管理页关闭插件后，Hooks、Rules 与 Skill 会一次性全部停用，对全局配置零污染。

---

## 自定义配置

插件开箱即用，默认数据目录为 `~/.gemini/antigravity-session-vault/`。

如需自定义保险库路径（如跨盘存放到 `D:\AntigravityVault`）或留存阈值，可初始化并编辑 `~/.gemini/antigravity-session-vault/config.json`（或通过 `SESSION_VAULT_CONFIG` 环境变量指定路径）：

```bash
python3 -B -m session_vault config --init   # Windows: python -B -m session_vault config --init
```

```json
{
  "data_dir": "~/.gemini/antigravity",
  "archive_dir": "~/.gemini/antigravity-session-vault/archive",
  "vault_dir": "~/.gemini/antigravity-session-vault/vault",
  "log_file": "~/.gemini/antigravity-session-vault/session-vault.log",
  "lock_dir": "~/.gemini/antigravity-session-vault/locks",
  "retention": {
    "target_max": 420,
    "root_active_days": 2,
    "keep_idle_days": 3,
    "recent_touch_guard_sec": 300,
    "keep_roots": [],
    "archive_roots": [],
    "keep_subagents": []
  },
  "watcher": {
    "shield_interval_sec": 120,
    "backup_interval_sec": 1800
  }
}
```

配置在加载阶段执行 Fail-Fast 校验：整数字段传入布尔值、阈值越界、配置段非 JSON 对象或空路径覆盖都会立即抛出 `ValueError`，同时保持 `_sec` 时间键名完全兼容。

---

## 常用命令

在插件目录下运行（或执行 `pip install -e .` 注册全局 `session-vault` 命令）。所有子命令均支持全局参数 `--config <path>`、`--data-dir <path>`、`--archive-dir <path>`、`--vault-dir <path>`、`--version`。只读命令（`status`、`search`、`clean --dry-run`、`restore --list`、`config` 无 `--init`）保证**零日志文件与日志目录副作用**：

| 命令 | 可在 AGY 运行时执行？ | 说明 |
|---|---|---|
| `python -B -m session_vault status` | ✅ 可以（只读） | 查看活跃 DB 数、`PruneAll(500)` 风险模拟、守护进程状态、幽灵数与保险库统计 |
| `python -B -m session_vault trees [--json] [--limit N]` | ✅ 可以（只读） | 按主对话汇总活跃/已归档子会话数量、体积与 `keep`/`archive`/`auto` 策略标签 |
| `python -B -m session_vault policy [list \| keep \| archive \| auto <id...>] [--subagent]` | ✅ 可以 | 查看或调整主对话/子会话保留优先级（守护进程离线清理前自动热重读，无需重启） |
| `python -B -m session_vault search [关键词] [--root <ID>] [--limit N]` | ✅ 可以（只读） | 在线检索 `index.jsonl` 并输出归档子会话的 `transcript.jsonl` 路径 |
| `python -B -m session_vault clean --dry-run [--verbose] [--target-max N] [--root-active-days N] [--keep-idle-days N]` | ✅ 可以（只读） | 预览下次关闭 AGY 时的精简归档计划（加 `--verbose` 可附带完整 `offload_ids` 列表） |
| `python -B -m session_vault discard [--root <ID> \| <cid...>] [--yes]` | ✅ 可以（`--yes` 删冷归档） | 预览或显式删除不再需要的冷归档子会话（绝不碰活跃 DB 或主对话保险库） |
| `python -B -m session_vault shield` | ✅ 可以 | 手动执行第一层在线 `mtime` 护盾 |
| `python -B -m session_vault backup` | ✅ 可以 | 手动执行第三层主对话 WAL 安全增量备份 |
| `python -B -m session_vault ensure [--force]` | ✅ 可以 | 若守护进程未启动则按需拉起（`--force` 可在 AGY 未运行时强制拉起） |
| `python -B -m session_vault watch [--shield-sec N] [--backup-sec N] [--exit-after-clean]` | ✅ 可以 | 前台运行生命周期守护进程（在线护盾+备份，退出时离线归档） |
| `python -B -m session_vault clean [--target-max N] [--root-active-days N] [--keep-idle-days N]` | ⛔ 仅离线 | 执行离线智能归档与双库清幽灵（检测到 AGY 运行会自动拦截） |
| `python -B -m session_vault restore --list` | ✅ 可以（只读） | 列出保险库主对话与归档子会话清单（不带参数的 `restore` 行为相同） |
| `python -B -m session_vault restore <uuid>` | ⛔ 仅离线 | 将指定对话还原回 Antigravity Desktop 侧边栏（含元文件自动快照、`< 480` 活跃数防线与路径穿越校验） |
| `python -B -m session_vault config [--init]` | ✅ 可以（`--init` 写入） | 输出当前解析配置（加 `--init` 原子写入 `config.json`） |

---

## 运行测试

```bash
python -B -m unittest discover -s tests -v
```

测试全部在隔离临时目录运行，数秒即可跑完：
- 严格配置校验与字节级无损 Protobuf 编解码（完整保留 Field 2 / CLI 记录与未知字段）
- 8 类会话分类器、四级最旧优先留存与 420 安全阀，以及第一层在线 `mtime` 护盾
- 脏 WAL 恢复、SQLite + FTS5 完整性校验、`PermissionError` 原子回滚、跨盘迁移、脏跳过增量备份与离线还原（含路径穿越与 `< 480` 防线）
- 并发原子写盘、跨平台内核单例锁，以及真实 `KILL_ON_JOB_CLOSE` + `taskkill /T /F`（Windows）/`SIGKILL`（POSIX）脱离存活验证
- 端到端命令行闭环、只读命令零日志、守护进程重启宽限与快路径 Hook 契约

---

## 许可证

[MIT](./LICENSE)

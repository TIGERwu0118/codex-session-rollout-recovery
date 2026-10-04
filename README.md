# codex-session-rollout-recovery

**Recover deleted or broken OpenAI Codex session rollout files — rebuilt from Codex's own internal database. / 从 Codex 内部数据库重建已删除或损坏的会话文件。**

[English](#english) | [中文](#中文)

---

<a id="english"></a>

## English

This is a battle-tested recovery toolkit + knowledge base for OpenAI **Codex** (CLI / Desktop) session data. It was validated on a real incident: **6.3 GB of session files under `~/.codex/sessions/` were deleted with `rm`**, local Time Machine snapshots had already been purged, and the external backup did not cover the deletion window. Full conversation content was nevertheless recovered — because Codex keeps a **content projection database** (`thread_history_1.sqlite`) that survives file deletion.

### Why it works

Codex session data lives in three places:

1. **`sessions/YYYY/MM/DD/rollout-*.jsonl`** — the rollout files (one JSON object per line).
2. **`thread_history_1.sqlite` → `thread_items`** — a projection of every message, reasoning summary and tool call (`item_json`). **This survives file deletion.**
3. **`state_5.sqlite` → `threads`** — the registry: authoritative `rollout_path` + real per-thread metadata (cwd, cli_version, source…).

Rebuild (2) into the file format of (1), at the exact path from (3), then align the validation checkpoints — and Codex opens the session again.

### The 6 hard validations (each error = one violated rule)

| Codex error | Root cause |
|---|---|
| `does not start with session metadata` | First line invalid, **or `payload.timestamp` missing the `Z` suffix** |
| `belongs to thread X, expected Y` | `session_meta.id` ≠ `threads.id` (classic fork mistake) |
| `failed to resolve rollout path … file does not exist` | File missing at the registered path — name must match character-for-character |
| `cutoff byte offset is past the source rollout` | Stored projection offset exceeds rebuilt file size |
| `missing source rollout` / `invalid paginated history lineage` | Multi-page lineage chain references a deleted page |
| Short rebuilt file silently rejected | Later pages start at ordinal N ⇒ the source page must actually have ≥ N records |

**Core insight: never re-declare lineage.** Multi-page (forked/paginated) sessions are consolidated into ONE standalone single-page rollout — all pages' items merged (including items stored under fork ids), no lineage references — written to the registered path.

### Usage

```bash
git clone https://github.com/TIGERwu0118/codex-session-rollout-recovery.git
cd codex-session-rollout-recovery

# Quit Codex first, then:
python3 scripts/codex_recover.py inventory            # three-way diff: registry vs disk vs DB content
python3 scripts/codex_recover.py doctor --dry-run     # preview the repairs
python3 scripts/codex_recover.py doctor               # rebuild + align checkpoints
python3 scripts/codex_recover.py verify               # re-check every validation rule
```

- Replaced originals are always moved to `~/codex_recovery_backup/` first — **healthy files are never touched**.
- Python 3 stdlib only (no pip installs).
- `CODEX_HOME` env var is respected if your Codex home is elsewhere.

### What is / isn't recoverable

| ✅ Recoverable | ❌ Not in the DB |
|---|---|
| User & assistant messages, reasoning summaries, tool calls | Process events (`event_msg`), token counts |
| Forked-session content (items stored under fork ids) | History compacted away before deletion (only a Time Machine backup has those originals) |
| Registry metadata (cwd, cli_version, source) from `threads` | Threads with zero projected items (a valid empty shell is written so they open blank instead of erroring) |

### Also usable as a Claude/ZCode skill

`SKILL.md` follows the skill format — drop this folder into your skills directory and the agent can perform the whole procedure (including the error-message triage table) when asked to recover Codex sessions.

---

<a id="中文"></a>

## 中文

这是一套经过真实事故完整验证的 **OpenAI Codex**（CLI / 桌面版）会话恢复工具与知识库。验证场景：`~/.codex/sessions/` 下 **6.3GB 会话文件被 `rm` 删除**，本地 Time Machine 快照已清空、外置备份不覆盖删除窗口——对话内容依然全部救回，因为 Codex 自带一个**内容投影数据库**（`thread_history_1.sqlite`），文件删了它还在。

### 原理

Codex 会话数据存在三处：

1. **`sessions/年/月/日/rollout-*.jsonl`** —— 会话原文文件（每行一个 JSON 对象）；
2. **`thread_history_1.sqlite` → `thread_items`** —— 每条消息/推理摘要/工具调用的投影（`item_json` 字段），**文件删除后依然幸存**；
3. **`state_5.sqlite` → `threads`** —— 注册表：权威的 `rollout_path` 和每个会话的真实元数据（cwd、cli_version、source…）。

把 (2) 重建为 (1) 的文件格式、写到 (3) 指定的原路径、再对齐校验检查点——会话就能重新打开。

### 六条硬校验（一个报错 = 一条被违反的规则）

| Codex 报错 | 根因 |
|---|---|
| `does not start with session metadata` | 第 1 行非法，**或 `payload.timestamp` 缺 `Z` 后缀**（极易踩） |
| `belongs to thread X, expected Y` | `session_meta.id` ≠ `threads.id`（续接会话最易犯） |
| `failed to resolve rollout path … file does not exist` | 注册路径上文件缺失，文件名必须逐字符一致 |
| `cutoff byte offset is past the source rollout` | 投影偏移超过重建文件大小 |
| `missing source rollout` / `invalid paginated history lineage` | 多页血统链引用了已删页 |
| 短重建文件被静默拒绝 | 后续页 ordinal 从 N 起 ⇒ 前一页必须真有 ≥N 条记录 |

**核心心法：血统声明一律不写。** 多页（续接/分页）会话合并为**一个自洽单页**——所有分页内容（含挂在续接 ID 名下的消息）合并、不带任何血统引用——写到注册表指定路径。

### 使用

```bash
git clone https://github.com/TIGERwu0118/codex-session-rollout-recovery.git
cd codex-session-rollout-recovery

# 先退出 Codex，然后：
python3 scripts/codex_recover.py inventory            # 三方对账：注册表 vs 磁盘 vs 库内容
python3 scripts/codex_recover.py doctor --dry-run     # 预览将要做的修复
python3 scripts/codex_recover.py doctor               # 重建 + 对齐校验点
python3 scripts/codex_recover.py verify               # 复检全部校验规则
```

- 被替换的原件一律先移入 `~/codex_recovery_backup/`——**健康文件绝不触碰**；
- 纯 Python 3 标准库，无需 pip；
- Codex 主目录不在默认位置时可用 `CODEX_HOME` 环境变量指定。

### 能恢复 / 不能恢复

| ✅ 能 | ❌ 库里没有 |
|---|---|
| 用户与助手消息、推理摘要、工具调用 | 过程事件（`event_msg`）、token 计数 |
| 续接会话内容（挂在续接 ID 名下的消息） | 删除前已被压缩掉的历史（只有 Time Machine 备份有原文） |
| 注册表里的元数据（cwd、cli_version、source） | 投影为空的线程（生成合法空壳，打开显示空白而非报错） |

### 也可作为 Agent 技能使用

`SKILL.md` 遵循技能格式——把本目录放进你的 skills 目录，Agent 即可按文档（含报错分诊表）执行整套恢复流程。

---

## License / 许可

MIT — see [LICENSE](LICENSE).

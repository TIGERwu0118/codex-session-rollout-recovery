---
name: codex-session-rollout-recovery
description: Recover deleted or broken OpenAI Codex CLI/Desktop session rollout files by rebuilding them from Codex's internal content database (thread_history_1.sqlite) with registry-aligned validation. Use when Codex sessions are missing, or when opening a session fails with errors like "does not start with session metadata", "failed to resolve rollout path", "cutoff byte offset is past the source rollout", "missing source rollout", or "belongs to thread X, expected Y". 通过 Codex 内部内容库重建已删除/损坏的会话 rollout 文件。
---

# Codex Session Rollout Recovery / Codex 会话恢复技能

Bilingual (EN/中文). Battle-tested on a real incident: 6.3 GB of session files (`~/.codex/sessions/`) deleted with `rm`, no Time Machine snapshot covering the window — full conversation content recovered from Codex's own projection database.

双语文档。在一次真实事故上完整验证：`~/.codex/sessions/` 下 6.3GB 会话文件被 `rm` 删除、时间机器快照不覆盖删除窗口——最终从 Codex 自带的内容投影数据库完整恢复了对话内容。

## 0. Architecture: where Codex session data actually lives / 会话数据的真实存储结构

All under `~/.codex/` (override with `CODEX_HOME`):

| File / Table 文件/表 | Role 角色 |
|---|---|
| `thread_history_1.sqlite` → `thread_items` | **Content lifeline.** Every user/assistant message, reasoning summary, tool call — as `item_json`. Even if every rollout file is deleted, content survives here. **内容生命线**：全部对话内容都在 `item_json` 字段里，rollout 文件全删也能从这里重建 |
| `state_5.sqlite` → `threads` | **Registry & source of truth.** `id` + `rollout_path` (the exact file Codex opens) + real cwd/cli_version/source per thread. **注册表**：`rollout_path` 是 Codex 打开文件的权威路径 |
| `thread_history_1.sqlite` → `thread_history_projection_state` | Validation checkpoint: `next_rollout_byte_offset` / `next_rollout_ordinal` must be ≤ file size / record count. **校验关卡**：偏移必须不超过文件实际大小 |
| `thread_history_1.sqlite` → `thread_turns` | Per-turn byte offsets; also need clamping. 每轮字节偏移，同样需要对齐 |
| `sessions/YYYY/MM/DD/rollout-<ts>-<threadID>[_<forkID>].jsonl` | The rollout files themselves. One JSON object per line. 会话原文文件，逐行 JSON |
| `logs_2.sqlite` | Logs contain **original rollout paths** — the only way to recover exact original filenames (incl. fork naming `parent_fork`). 日志里存有原始文件路径，是找回准确文件名的唯一途径 |
| `session_index.jsonl`, `archived_sessions/` | Session titles index; archived copies. 会话名索引与归档副本 |

## 1. Rollout file format — the 6 hard validations / 文件格式与六条硬校验

First line MUST be `session_meta` with **all 13 required fields**; `payload.timestamp` MUST end with `Z`; `payload.id` MUST equal `threads.id`:

```json
{"timestamp":"2026-09-29T02:27:45.239Z","ordinal":0,"type":"session_meta","payload":{
  "session_id":"<threads.id>","id":"<threads.id>","timestamp":"2026-09-29T02:27:45.239Z",
  "cwd":"/path/to/project","runtime_workspace_roots":["/path/to/project"],
  "originator":"Codex Desktop","cli_version":"0.159.0","source":"vscode",
  "thread_source":"user","model_provider":"openai",
  "base_instructions":{"text":"...","provenance":{...}},
  "history_mode":"paginated","context_window":{"window_id":"..."}}}
```

Following lines: `{"timestamp":"...Z","ordinal":N,"type":"response_item","payload":{...}}`, compact JSON (no spaces). DB types `userMessage`/`agentMessage` convert to `{"type":"message","role":"user|assistant","content":[{"type":"text","text":"..."}]}`.

Every error message maps to exactly one violated rule — use this table to triage / 每个报错对应一条被违反的规则，按下表分诊：

| Error 报错 | Root cause 根因 |
|---|---|
| `does not start with session metadata` | First line not valid meta, **or `timestamp` missing `Z` suffix** (easy to miss!) / 第 1 行非法，或 timestamp 缺 Z 后缀（极易踩） |
| `belongs to thread X, expected Y` | meta `id` ≠ `threads.id` — classic fork mistake / 续接会话最易犯：id 用错 |
| `failed to resolve rollout path ... file does not exist` | File missing at `threads.rollout_path`; name must match **character-for-character** / 文件名必须逐字符一致 |
| `cutoff byte offset is past the source rollout` | `projection_state` offset > rebuilt file size / 投影偏移超过重建文件大小 |
| `missing source rollout` / `invalid paginated history lineage` | Multi-page lineage chain references a deleted page / 分页链引用了已删页 |
| short rebuilt file silently rejected | Later pages start at ordinal N ⇒ source page must really have ≥N records / 短文件过不了链校验 |

## 2. Recovery procedure (validated) / 恢复流程（实战验证版）

**Step 0 — Stop-loss / 止血.** Quit Codex. Copy `thread_history_1.sqlite` (+`-wal`/`-shm`), `state_5.sqlite`, `logs_2.sqlite`, and any surviving `sessions/` to a rescue folder. Never experiment on originals. 退出 Codex；把三个库和幸存文件先复制出一份，绝不在原件上试错。

**Step 1 — Inventory / 盘点.** Three lists: ① all rows in `threads` (id + rollout_path); ② files on disk (thread id from filename); ③ threads with content in `thread_items`. Diff them. 三份清单对差：注册表 / 盘上文件 / 库内内容。

**Step 2 — Template / 模板.** Extract `base_instructions` from ANY surviving original (reusable globally); take per-thread cwd/originator/cli_version/source from `threads` itself (never guess). Recover exact original filenames from `logs_2.sqlite` (`feedback_log_body LIKE '%sessions/%rollout-%'`).

**Step 3 — Rebuild as a STANDALONE SINGLE PAGE / 重建为自洽单页.** The core insight: **never re-declare lineage**. For multi-page threads, merge ALL pages' items — including items stored under fork ids — into ONE file written to `threads.rollout_path`, with `id = threads.id`. 核心心法：血统声明一律不写；多页会话把所有分页内容（含挂在续接 ID 名下的消息）合并成一个单页，写到注册表指定的原路径。

**Step 4 — Align checkpoints / 对齐校验点.** `next_rollout_byte_offset` = actual new file size; `next_rollout_ordinal` = record count; clamp `thread_turns` offsets to file size; delete stale fork-id projection rows. 偏移全部对齐到新文件；删掉挂错 ID 的投影行。

**Step 5 — Verify iteratively / 验证迭代.** Restart Codex, click through sessions, triage any error via the table above. Every replaced original goes to backup first. 被替换原件永远先进备份目录。

**Guard rails / 红线**: files that are healthy (`exists && size ≥ cutoff && valid meta && no dangling lineage refs`) are NEVER touched. 健康文件绝不触碰。

## 3. What is and isn't recoverable / 能恢复与不能恢复

- ✅ 100%: conversation content (user/assistant messages, reasoning summaries, tool calls) present in `thread_items`.
- ❌ Not in the DB: process events (`event_msg`), token counts, compacted-away early history (only TM backup has the originals), threads with zero projected items (subagent shells — write a valid empty file so they open blank instead of erroring).
- Backup priority: TM snapshot/disk (only place with full originals) > `thread_history_1.sqlite` (content fallback) > `sessions/`.

## 4. Run it / 运行

```bash
# 0) quit Codex first, then:
python3 scripts/codex_recover.py inventory            # three-way diff report
python3 scripts/codex_recover.py doctor --dry-run     # preview repairs
python3 scripts/codex_recover.py doctor               # rebuild + align (originals → ~/codex_recovery_backup/)
python3 scripts/codex_recover.py verify               # re-check all validation rules
```

Requires only Python 3 stdio (sqlite3, json) — no third-party packages.

## 5. Prevention / 防再犯

Before deleting anything under `~/.codex`, run `inventory` and see how many threads the registry expects; deleting files without the registry corrupts opens. Consider backing up `thread_history_1.sqlite` + `sessions/` (~2 GB typical) before any cleanup. 清理 `~/.codex` 前先跑 `inventory` 对账；动手前至少备份内容库。

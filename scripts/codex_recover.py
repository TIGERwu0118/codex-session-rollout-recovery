#!/usr/bin/env python3
"""
codex_recover.py — Codex session rollout recovery toolkit (battle-tested 2026-10).

Recovers deleted or broken Codex session rollout files by rebuilding them from
Codex's own content-projection database (thread_history_1.sqlite), using the
thread registry (state_5.sqlite) as the source of truth for paths and metadata,
and aligning the projection checkpoints so Codex's validation passes.

Subcommands:
  inventory   Three-way diff: threads registry vs disk files vs DB content
  doctor      Full auto-repair: rebuild missing/broken rollouts + align checkpoints
  verify      Re-check every registered thread against the validation rules

All writes are guarded: healthy files are never touched, originals are moved
to a backup directory before being replaced, and Codex should be quit first.

Usage:
  python3 codex_recover.py inventory
  python3 codex_recover.py doctor [--dry-run]
  python3 codex_recover.py verify
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
STATE_DB = os.path.join(CODEX_HOME, "state_5.sqlite")
HISTORY_DB = os.path.join(CODEX_HOME, "thread_history_1.sqlite")
LOGS_DB = os.path.join(CODEX_HOME, "logs_2.sqlite")
SESSIONS_DIR = os.path.join(CODEX_HOME, "sessions")
BACKUP_DIR = os.path.expanduser("~/codex_recovery_backup")

ID_RE = re.compile(
    r"rollout-.*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))?\.jsonl$"
)
PATH_RE = re.compile(r"""sessions/\d{4}/\d{2}/\d{2}/rollout-[^ ,'"）)]+\.jsonl""")

# ---------------------------------------------------------------- helpers

def iso_ms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


class Ctx:
    """Opens everything we need. history_db is opened read-write for alignment."""

    def __init__(self):
        self.st = sqlite3.connect(STATE_DB)
        self.hist = sqlite3.connect(HISTORY_DB)
        self.logs = ro(LOGS_DB) if os.path.exists(LOGS_DB) else None
        self.base_instructions = None
        self.fork_map = {}      # parent thread id -> set of fork thread ids
        self.orig_names = {}    # thread id -> original basename (from logs)

    def load_template(self):
        """Extract reusable base_instructions from any healthy session_meta."""
        for root, _dirs, files in os.walk(SESSIONS_DIR):
            for f in sorted(files):
                if not f.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(root, f), errors="replace") as fh:
                        meta = json.loads(fh.readline())
                    bi = meta["payload"].get("base_instructions")
                    if meta.get("type") == "session_meta" and bi:
                        self.base_instructions = bi
                        return True
                except Exception:
                    continue
        return False

    def load_forks_and_names(self):
        if not self.logs:
            return
        for (body,) in self.logs.execute(
            "SELECT feedback_log_body FROM logs "
            "WHERE feedback_log_body LIKE '%sessions/%rollout-%'"
        ):
            if not body:
                continue
            for p in PATH_RE.findall(body):
                m = ID_RE.search(p)
                if not m:
                    continue
                parent, fork = m.group(1), m.group(2)
                gid = fork or parent
                self.orig_names.setdefault(gid, os.path.basename(p))
                if fork and fork != parent:
                    self.fork_map.setdefault(parent, set()).add(fork)

    def thread_row(self, tid):
        return self.st.execute(
            "SELECT rollout_path, cwd, originator, cli_version, source "
            "FROM threads WHERE id=?", (tid,)
        ).fetchone()

    def registered_threads(self):
        return self.st.execute(
            "SELECT id, rollout_path, cwd, originator, cli_version, source FROM threads"
        ).fetchall()

    def items_for(self, tid, merge_forks=True):
        """All content items for a thread, merging fork-thread items when present."""
        ids = [tid]
        if merge_forks:
            ids += sorted(self.fork_map.get(tid, []))
        items = []
        for x in ids:
            items += self.hist.execute(
                "SELECT item_json, created_at_ms FROM thread_items "
                "WHERE thread_id=? ORDER BY created_at_ms, rollout_ordinal", (x,)
            ).fetchall()
        items.sort(key=lambda r: r[1])
        return items


# ------------------------------------------------------------ validation

def check_file(ctx, tid, rollout_path):
    """Returns (status, detail). status: healthy|missing|broken|empty"""
    if not os.path.exists(rollout_path):
        return "missing", "file does not exist"
    row = ctx.hist.execute(
        "SELECT next_rollout_byte_offset, next_rollout_ordinal "
        "FROM thread_history_projection_state WHERE thread_id=?", (tid,)
    ).fetchone()
    cutoff, next_ord = row if row else (0, 0)
    size = os.path.getsize(rollout_path)
    if size == 0:
        return "empty", "zero bytes"
    try:
        with open(rollout_path, errors="replace") as fh:
            first = json.loads(fh.readline())
        if first.get("type") != "session_meta":
            return "broken", "first line is not session_meta"
        p = first.get("payload", {})
        if not str(p.get("timestamp", "")).endswith("Z"):
            return "broken", "payload.timestamp missing Z suffix"
        if p.get("id") != tid:
            return "broken", f"meta id {p.get('id')} != threads.id {tid}"
    except Exception as e:
        return "broken", f"unparseable first line: {e}"
    if cutoff and cutoff > size:
        return "broken", f"cutoff offset {cutoff} > file size {size}"
    body = open(rollout_path, errors="replace").read()
    for ref in set(PATH_RE.findall(body.replace("\\", " "))):
        m = ID_RE.search(ref)
        if m and m.group(1) != tid:
            ref_path = os.path.join(CODEX_HOME, ref)
            if not os.path.exists(ref_path):
                return "broken", f"lineage references missing source: {ref}"
    return "healthy", ""


# -------------------------------------------------------------- builders

def convert_item(raw_json):
    p = json.loads(raw_json)
    t = p.get("type")
    if t in ("userMessage", "agentMessage"):
        role = "user" if t == "userMessage" else "assistant"
        return {
            "type": "message", "role": role,
            "content": [
                {"type": "text", "text": c.get("text", ""), "text_elements": []}
                for c in p.get("content", []) if c.get("type") == "text"
            ],
        }
    return p  # reasoning / tool calls / others pass through


def write_standalone(ctx, tid, rollout_path, cwd, originator, cli_version,
                     source, items, backup=True, dry=False):
    """Write a self-consistent single-page rollout with NO lineage declaration.
    Original file (if any) is moved to BACKUP_DIR first. Returns bytes written."""
    if dry:
        return 0
    if os.path.exists(rollout_path) and backup:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.move(rollout_path, os.path.join(BACKUP_DIR, os.path.basename(rollout_path)))
    start_ms = items[0][1] if items else 0
    payload = {
        "session_id": tid, "id": tid,                       # MUST equal threads.id
        "timestamp": iso_ms(start_ms),                      # MUST end with Z
        "cwd": cwd, "runtime_workspace_roots": [cwd],
        "originator": originator, "cli_version": cli_version,
        "thread_source": "user", "source": source,
        "model_provider": "openai",
        "base_instructions": ctx.base_instructions,         # 13 required fields complete
        "history_mode": "paginated",
        "context_window": {"window_id": str(uuid.uuid5(uuid.NAMESPACE_URL, tid))},
    }
    os.makedirs(os.path.dirname(rollout_path), exist_ok=True)
    with open(rollout_path, "w") as f:
        f.write(json.dumps({"timestamp": iso_ms(start_ms), "ordinal": 0,
                            "type": "session_meta", "payload": payload},
                           ensure_ascii=False, separators=(",", ":")) + "\n")
        for i, (raw, ms) in enumerate(items, 1):
            try:
                item = convert_item(raw)
            except Exception:
                continue
            f.write(json.dumps({"timestamp": iso_ms(ms), "ordinal": i,
                                "type": "response_item", "payload": item},
                               ensure_ascii=False, separators=(",", ":")) + "\n")
    return os.path.getsize(rollout_path)


def align_projection(ctx, tid, fsize, max_ord, fork_ids=()):
    """Clamp/insert projection checkpoints to match the rebuilt file."""
    row = ctx.hist.execute(
        "SELECT 1 FROM thread_history_projection_state WHERE thread_id=?", (tid,)
    ).fetchone()
    if row:
        ctx.hist.execute(
            "UPDATE thread_history_projection_state "
            "SET next_rollout_byte_offset=?, next_rollout_ordinal=? WHERE thread_id=?",
            (fsize, max_ord, tid))
    else:
        ctx.hist.execute(
            "INSERT INTO thread_history_projection_state "
            "(thread_id, next_rollout_byte_offset, next_rollout_ordinal) VALUES (?,?,?)",
            (tid, fsize, max_ord))
    ctx.hist.execute(
        "UPDATE thread_turns SET rollout_byte_offset=? WHERE thread_id=? AND rollout_byte_offset>?",
        (fsize, tid, fsize))
    ctx.hist.execute(
        "UPDATE thread_turns SET rollout_end_byte_offset=? WHERE thread_id=? AND rollout_end_byte_offset>?",
        (fsize, tid, fsize))
    for fid in fork_ids:  # stale fork checkpoints only confuse validation
        ctx.hist.execute("DELETE FROM thread_history_projection_state WHERE thread_id=?", (fid,))


# ------------------------------------------------------------ subcommands

def cmd_inventory(ctx):
    rows = ctx.registered_threads()
    n = {"healthy": 0, "missing": 0, "broken": 0, "empty": 0, "no_content": 0}
    problems = []
    for tid, rp, *_ in rows:
        status, detail = check_file(ctx, tid, rp)
        n[status] = n.get(status, 0) + 1
        if status != "healthy":
            problems.append((status, tid, rp, detail))
    print(f"registered threads : {len(rows)}")
    for k in ("healthy", "missing", "broken", "empty"):
        print(f"  {k:8s}        : {n[k]}")
    for status, tid, rp, detail in problems[:30]:
        print(f"  [{status}] {tid}\n           {detail}\n           {rp}")
    return problems


def cmd_doctor(ctx, dry=False):
    if not ctx.load_template():
        sys.exit("FATAL: no healthy rollout found to extract base_instructions from")
    ctx.load_forks_and_names()
    stats = {"rebuilt": 0, "shells": 0, "kept_healthy": 0, "failed": 0}
    for tid, rp, cwd, orig, cliv, src in ctx.registered_threads():
        status, _ = check_file(ctx, tid, rp)
        if status == "healthy":
            stats["kept_healthy"] += 1
            continue
        fork_ids = sorted(ctx.fork_map.get(tid, []))
        items = ctx.items_for(tid)
        # content may live under a fork id even when parent has none
        if not items:
            for fid in fork_ids:
                items = ctx.items_for(fid, merge_forks=False)
                if items:
                    break
        if dry:
            print(f"[dry] would rebuild {tid} ({len(items)} items) -> {rp}")
            continue
        size = write_standalone(ctx, tid, rp, cwd, orig, cliv, src, items)
        align_projection(ctx, tid, size, max(len(items), 1), fork_ids)
        stats["rebuilt" if items else "shells"] += 1
    ctx.hist.commit()
    print("repair summary:", stats)
    print(f"originals backed up under: {BACKUP_DIR}")
    print("restart Codex now, then run: python3 codex_recover.py verify")


def cmd_verify(ctx):
    bad = 0
    for tid, rp, *_ in ctx.registered_threads():
        status, detail = check_file(ctx, tid, rp)
        if status != "healthy":
            bad += 1
            print(f"[{status}] {tid}: {detail}")
    print(f"{'ALL PASS' if bad == 0 else f'{bad} problem(s) remain'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["inventory", "doctor", "verify"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for db in (STATE_DB, HISTORY_DB):
        if not os.path.exists(db):
            sys.exit(f"FATAL: {db} not found — is CODEX_HOME correct?")
    ctx = Ctx()
    if args.command == "inventory":
        cmd_inventory(ctx)
    elif args.command == "doctor":
        cmd_doctor(ctx, args.dry_run)
    else:
        cmd_verify(ctx)


if __name__ == "__main__":
    main()

import fnmatch
import hmac
import json
import logging
import os
import re
import shlex
import sqlite3
import ssl
import stat
import sys
import threading
import urllib.parse
import uuid
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.env_config import load_environment
from core.neutral_terminal import NeutralTerminal

_SESSION_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_HIGH_WATER = 4096


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is not None:
            return lock
        if len(_SESSION_LOCKS) >= _SESSION_LOCKS_HIGH_WATER:
            stale = [sid for sid, lk in _SESSION_LOCKS.items() if not lk.locked()]
            for sid in stale:
                del _SESSION_LOCKS[sid]
                if len(_SESSION_LOCKS) < _SESSION_LOCKS_HIGH_WATER:
                    break
        lock = threading.Lock()
        _SESSION_LOCKS[session_id] = lock
        return lock

logger = logging.getLogger("jala.server")

SYSTEM_PROMPT = (
    "You are a helpful conversational terminal assistant. "
    "The user provides their current working directory with each turn in a "
    "separate system message. Use prior conversation context when answering "
    "follow-up questions.\n\n"
    "## Tool usage policy\n\n"
    "Always prefer the structured read-only tools over run_shell_command for inspection tasks. "
    "Structured tools are safer, validated, and do not require user approval. "
    "Only use run_shell_command when no structured tool can satisfy the request, "
    "or when you need to modify the user's local environment. "
    "State-changing shell commands always require explicit user approval.\n\n"
    "## Structured tools and when to use them\n\n"
    "- read_file: read the contents of a specific file. Use instead of `cat`, `head`, or `tail`.\n"
    "- list_directory: list the contents of a directory. Use instead of `ls`.\n"
    "- search_files: find files by name or glob pattern. Use instead of `find -name`.\n"
    "- search_file_contents: search for text or regex patterns inside files. Use instead of `grep` or `rg`.\n"
    "- file_metadata: get size, permissions, type, and modification time of a file or directory. Use instead of `stat` or `file`.\n"
    "- inspect_process: inspect a single process by PID. Use instead of `ps -p <pid>`.\n"
    "- list_processes: list running processes, optionally filtered by name. Use instead of `ps aux` or `pgrep`.\n"
    "- git_inspect: run a read-only git subcommand (status, diff, log, show, branch, rev-parse, remote, ls-files). "
    "Use instead of running git directly via run_shell_command.\n\n"
    "## When structured tools fail\n\n"
    "If a structured tool returns an error, read the error message carefully. "
    "Validation errors (e.g. missing argument, wrong type) indicate a tool call mistake you should fix. "
    "Execution errors (e.g. file not found, permission denied) reflect real environment state. "
    "Do not fall back to run_shell_command just because a structured tool returned an error — "
    "only fall back if the structured tool genuinely cannot fulfil the request."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Execute a shell command in the user's working directory. Free-form shell execution always requires approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Use instead of 'cat', 'head', or 'tail'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read. May be absolute or relative to cwd.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Find files by name or glob pattern. Use instead of 'find -name'. To search file contents, use search_file_contents instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The filename or glob pattern to match, e.g. '*.py' or 'config.json'.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "The directory to search in. Defaults to the current working directory.",
                    }
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_process",
            "description": "Inspect a single running process by its PID. Use instead of 'ps -p <pid>'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {
                        "type": "integer",
                        "description": "Process ID to inspect.",
                    }
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the contents of a directory. Use instead of 'ls'. Returns file names, types, sizes, and permissions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list. Defaults to the current working directory.",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "Whether to include hidden files (names starting with '.'). Defaults to false.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_file_contents",
            "description": (
                "Search for a text or regex pattern inside files. Use instead of 'grep' or 'rg'. "
                "Returns matching lines with file paths and line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The text or regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search in. Defaults to the current working directory.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional glob pattern to restrict which files are searched, e.g. '*.py' or '**/*.md'.",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Whether to perform a case-insensitive search. Defaults to false.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching lines to return. Defaults to 100.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_metadata",
            "description": (
                "Get metadata for a file or directory: size, permissions, type, owner, and modification time. "
                "Use instead of 'stat' or 'file'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file or directory.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": (
                "List running processes. Use instead of 'ps aux' or 'pgrep'. "
                "Optionally filter by process name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name_filter": {
                        "type": "string",
                        "description": "Optional substring to filter process names. Returns all processes if omitted.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_inspect",
            "description": (
                "Run a read-only git subcommand in the current working directory. "
                "Supported subcommands: status, diff, log, show, branch, rev-parse, remote, ls-files. "
                "Use instead of running git directly via run_shell_command for inspection tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subcommand": {
                        "type": "string",
                        "description": "The git subcommand to run (e.g. 'status', 'log', 'diff').",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional additional arguments for the git subcommand, e.g. ['--oneline', '-10'].",
                    },
                },
                "required": ["subcommand"],
            },
        },
    },
]

MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 200
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_MODEL_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_MAX_CONCURRENT_REQUESTS = 16
DEFAULT_MAX_FILE_READ_BYTES = 256 * 1024

INSPECTION_TOOL_NAMES = frozenset({
    "read_file",
    "search_files",
    "inspect_process",
    "list_directory",
    "search_file_contents",
    "file_metadata",
    "list_processes",
    "git_inspect",
})
HISTORY_REMOVE_LAST_EVENT = "_history_remove_last"
READ_ONLY_COMMANDS = {
    "pwd",
    "ls",
    "cat",
    "head",
    "tail",
    "stat",
    "file",
    "wc",
    "du",
    "basename",
    "dirname",
    "readlink",
    "realpath",
    "which",
    "whereis",
    "env",
    "printenv",
    "id",
    "whoami",
    "date",
    "uname",
    "rg",
    "grep", "echo",
}
READ_ONLY_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "rev-parse",
    "remote",
    "ls-files",
}

# Per-subcommand flag allowlists for git_inspect.
# Each entry maps a subcommand to the set of flags/options that are explicitly
# permitted. Plain non-flag arguments (paths, refs, commit hashes) are always
# allowed. Any flag NOT in this set is rejected. This prevents flag-based
# escapes such as --output=<file>, --exec, --upload-pack, --receive-pack, etc.
# "remote" only permits the read-only "show" and "get-url" sub-subcommands.
_GIT_ALLOWED_FLAGS: dict[str, frozenset[str]] = {
    "status": frozenset({
        "-s", "--short", "-b", "--branch", "--porcelain", "--long",
        "--ahead-behind", "--no-ahead-behind", "-u", "--untracked-files",
        "--ignored", "--no-ignored", "--renames", "--no-renames",
        "--find-renames", "-v", "--verbose",
    }),
    "diff": frozenset({
        "--stat", "--shortstat", "--name-only", "--name-status",
        "--cached", "--staged", "--diff-filter", "--no-color", "--color",
        "--ignore-space-change", "-b", "--ignore-all-space", "-w",
        "--ignore-blank-lines", "--unified", "-U", "--word-diff",
        "--word-diff-regex", "--minimal", "--patience", "--histogram",
        "--check", "--raw", "--numstat", "--dirstat", "--summary",
        "--no-index", "--exit-code", "--quiet", "-q",
        "-R", "--relative", "--no-relative",
        "--src-prefix", "--dst-prefix", "--no-prefix",
    }),
    "log": frozenset({
        "--oneline", "--decorate", "--no-decorate", "--graph",
        "--all", "--branches", "--tags", "--remotes",
        "-n", "--max-count", "--skip", "--since", "--after",
        "--until", "--before", "--author", "--committer", "--grep",
        "--all-match", "--invert-grep", "--regexp-ignore-case", "-i",
        "--extended-regexp", "-E", "--fixed-strings", "-F",
        "--merges", "--no-merges", "--first-parent", "--ancestry-path",
        "--reverse", "--topo-order", "--date-order", "--author-date-order",
        "--stat", "--shortstat", "--name-only", "--name-status",
        "--no-color", "--color", "--abbrev-commit", "--no-abbrev-commit",
        "--format", "--pretty", "--full-diff",
        "--left-right", "--cherry-mark", "--cherry-pick", "--cherry",
        "-p", "--patch", "--follow",
        "-S", "-G", "--pickaxe-regex", "--pickaxe-all",
    }),
    "show": frozenset({
        "--stat", "--shortstat", "--name-only", "--name-status",
        "--no-color", "--color", "--format", "--pretty",
        "-p", "--patch", "--no-patch",
        "--abbrev-commit", "--no-abbrev-commit",
    }),
    "branch": frozenset({
        "-a", "--all", "-r", "--remotes", "-l", "--list",
        "-v", "--verbose", "-vv",
        "--no-color", "--color", "--column", "--no-column",
        "--sort", "--points-at", "--merged", "--no-merged",
        "--contains", "--no-contains", "--format",
        "--show-current",
    }),
    "rev-parse": frozenset({
        "--abbrev-ref", "--symbolic", "--symbolic-full-name",
        "--verify", "--quiet", "-q", "--short",
        "--show-toplevel", "--show-prefix", "--show-cdup",
        "--is-inside-work-tree", "--is-inside-git-dir",
        "--is-bare-repository", "--is-shallow-repository",
        "--git-dir", "--absolute-git-dir",
        "--sq-quote", "--sq",
        "--branches", "--tags", "--remotes",
        "--all",
    }),
    "remote": frozenset({
        # Only read-only sub-subcommands are permitted.
        # "show" and "get-url" are safe; add/remove/set-url are mutating.
        "show", "get-url",
        "-v", "--verbose",
    }),
    "ls-files": frozenset({
        "-c", "--cached", "-d", "--deleted", "-m", "--modified",
        "-o", "--others", "-i", "--ignored", "-s", "--stage",
        "-u", "--unmerged", "-k", "--killed", "-t",
        "--directory", "--no-empty-directory", "--eol",
        "--exclude", "--exclude-from", "--exclude-per-directory",
        "--exclude-standard", "--full-name", "--error-unmatch",
        "--with-tree", "--abbrev", "-z",
        "--deduplicate",
    }),
}

# Flags that are unsafe regardless of subcommand.
_GIT_ALWAYS_BLOCKED_FLAGS: frozenset[str] = frozenset({
    "--output", "--exec", "--upload-pack", "--receive-pack",
    "--no-pager", "--paginate",  # pager can execute arbitrary commands
})
UNSAFE_FIND_FLAGS = {
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fprint",
    "-fprint0",
    "-fprintf",
}


class StatePersistenceError(RuntimeError):
    pass

class ToolLoopError(RuntimeError):
    pass


def _state_dir() -> str:
    override = os.environ.get("JALA_STATE_DIR")
    if override:
        return override

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return os.path.join(xdg_data_home, "jala")

    return os.path.join(os.path.expanduser("~/.local/share"), "jala")


def _state_db_path() -> str:
    return os.path.join(_state_dir(), "jala.db")


def _ensure_state_dir() -> None:
    os.makedirs(_state_dir(), exist_ok=True)

def _get_db_connection():
    conn = sqlite3.connect(_state_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

def load_state() -> None:
    _ensure_state_dir()
    with _get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT,
                event_data TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                session_id TEXT,
                message TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                session_id TEXT,
                cwd TEXT,
                tool_calls TEXT,
                status TEXT DEFAULT 'pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                error_text TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS command_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT,
                command TEXT,
                cwd TEXT,
                exit_code INTEGER,
                duration REAL,
                output TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

def _normalize_chat_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid JSON payload")

    message = payload.get("message")
    cwd = payload.get("cwd")
    session_id = payload.get("session_id")

    if not isinstance(message, str) or not message.strip():
        raise ValueError("Missing 'message'")
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("Missing 'cwd'")
    if session_id is None:
        session_id = "default"
    elif not isinstance(session_id, str):
        raise ValueError("Invalid 'session_id'")
    normalized_cwd = os.path.realpath(cwd.strip())
    if not os.path.isdir(normalized_cwd):
        raise ValueError("Invalid 'cwd'")

    return message.strip(), normalized_cwd, session_id.strip() or "default"


def _normalize_approval_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid JSON payload")

    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ValueError("Missing approval_id")

    return approval_id.strip()


def _load_session_event_rows(conn: sqlite3.Connection, session_id: str):
    return conn.execute(
        "SELECT id, session_id, timestamp, event_type, event_data"
        " FROM session_events WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()


def _reconstruct_session_messages(rows) -> list[dict]:
    messages: list[dict] = []
    for row in rows:
        event_id = row["id"]
        event_type = row["event_type"]
        event_data = row["event_data"]

        if event_type == HISTORY_REMOVE_LAST_EVENT:
            try:
                payload = json.loads(event_data)
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt session trim event id=%s", event_id)
                continue

            count = payload.get("count")
            if not isinstance(count, int) or count < 0:
                logger.warning("Skipping invalid session trim event id=%s payload=%r", event_id, payload)
                continue

            if count:
                del messages[max(0, len(messages) - count):]
            continue

        try:
            message = json.loads(event_data)
        except json.JSONDecodeError:
            logger.warning("Skipping corrupt session event id=%s type=%s", event_id, event_type)
            continue

        if not isinstance(message, dict):
            logger.warning("Skipping invalid session event id=%s type=%s", event_id, event_type)
            continue

        messages.append(message)

    return messages


def _append_session_message_event(conn: sqlite3.Connection, session_id: str, message: dict) -> None:
    conn.execute(
        "INSERT INTO session_events (session_id, event_type, event_data)"
        " VALUES (?, ?, ?)",
        (session_id, message.get("role", "unknown"), json.dumps(message)),
    )


def _append_session_trim_event(conn: sqlite3.Connection, session_id: str, count: int) -> None:
    if count <= 0:
        return
    conn.execute(
        "INSERT INTO session_events (session_id, event_type, event_data)"
        " VALUES (?, ?, ?)",
        (session_id, HISTORY_REMOVE_LAST_EVENT, json.dumps({"count": count})),
    )


def _session_history(session_id: str):
    with _get_db_connection() as conn:
        rows = _load_session_event_rows(conn, session_id)
    if not rows:
        return [{"role": "system", "content": SYSTEM_PROMPT}]

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    history.extend(_reconstruct_session_messages(rows))
    if len(history) > MAX_HISTORY_MESSAGES:
        history = [history[0]] + history[-(MAX_HISTORY_MESSAGES - 1):]
    return history


def _save_session_history(session_id: str, history):
    target_messages = history[1:]
    visible_limit = max(0, MAX_HISTORY_MESSAGES - 1)

    with _get_db_connection() as conn:
        existing_rows = _load_session_event_rows(conn, session_id)
        current_messages = _reconstruct_session_messages(existing_rows)
        visible_current = current_messages[-visible_limit:] if visible_limit else []

        shared_prefix = 0
        max_prefix = min(len(visible_current), len(target_messages))
        while shared_prefix < max_prefix and visible_current[shared_prefix] == target_messages[shared_prefix]:
            shared_prefix += 1

        remove_count = len(visible_current) - shared_prefix
        _append_session_trim_event(conn, session_id, remove_count)

        for message in target_messages[shared_prefix:]:
            _append_session_message_event(conn, session_id, message)

def _save_approval(approval_id: str, session_id: str, cwd: str, tool_calls, status: str = 'pending', error_text: str = None):
    with _get_db_connection() as conn:
        conn.execute("""
            INSERT INTO approvals (approval_id, session_id, cwd, tool_calls, status, error_text)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status, error_text=excluded.error_text
        """, (approval_id, session_id, cwd, json.dumps(tool_calls), status, error_text))

def _get_approval(approval_id: str):
    with _get_db_connection() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if row:
            try:
                tool_calls = json.loads(row["tool_calls"])
            except json.JSONDecodeError as error:
                raise StatePersistenceError(f"Corrupt approval payload for approval '{approval_id}'") from error
            return {
                "approval_id": row["approval_id"],
                "session_id": row["session_id"],
                "cwd": row["cwd"],
                "tool_calls": tool_calls,
                "status": row["status"],
            }
    return None

def _record_request(session_id: str, message: str):
    with _get_db_connection() as conn:
        conn.execute("""
            INSERT INTO requests (session_id, message)
            VALUES (?, ?)
        """, (session_id, message))

def _record_command_execution(approval_id: str, command: str, cwd: str, exit_code: int, duration: float, output: str):
    with _get_db_connection() as conn:
        conn.execute("""
            INSERT INTO command_executions (approval_id, command, cwd, exit_code, duration, output)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (approval_id, command, cwd, exit_code, duration, output))


def _command_execution_count(approval_id: str) -> int:
    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM command_executions WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
    return int(row["count"]) if row else 0


def _update_approval_status(approval_id: str, status: str, *, expected_status: str | None = None, error_text: str | None = None) -> bool:
    with _get_db_connection() as conn:
        if expected_status is None:
            cursor = conn.execute(
                "UPDATE approvals SET status = ?, error_text = ? WHERE approval_id = ?",
                (status, error_text, approval_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE approvals SET status = ?, error_text = ? WHERE approval_id = ? AND status = ?",
                (status, error_text, approval_id, expected_status),
            )
    return cursor.rowcount == 1


def _get_history_summary(session_id=None, event_type=None, start_time=None, end_time=None):
    with _get_db_connection() as conn:
        req_query = "SELECT * FROM requests WHERE 1=1"
        app_query = "SELECT * FROM approvals WHERE 1=1"
        cmd_query = "SELECT * FROM command_executions WHERE 1=1"
        ev_query = "SELECT * FROM session_events WHERE 1=1"

        req_params, app_params, cmd_params, ev_params = [], [], [], []

        if session_id:
            req_query += " AND session_id = ?"
            req_params.append(session_id)
            app_query += " AND session_id = ?"
            app_params.append(session_id)
            cmd_query = (
                "SELECT c.* FROM command_executions c"
                " JOIN approvals a ON c.approval_id = a.approval_id"
                " WHERE a.session_id = ?"
            )
            cmd_params.append(session_id)
            ev_query += " AND session_id = ?"
            ev_params.append(session_id)

        if event_type:
            ev_query += " AND event_type = ?"
            ev_params.append(event_type)

        if start_time:
            req_query += " AND datetime(timestamp) >= datetime(?)"
            req_params.append(start_time)
            app_query += " AND datetime(timestamp) >= datetime(?)"
            app_params.append(start_time)
            cmd_query += (
                " AND datetime(c.timestamp) >= datetime(?)" if session_id
                else " AND datetime(timestamp) >= datetime(?)"
            )
            cmd_params.append(start_time)
            ev_query += " AND datetime(timestamp) >= datetime(?)"
            ev_params.append(start_time)

        if end_time:
            req_query += " AND datetime(timestamp) <= datetime(?)"
            req_params.append(end_time)
            app_query += " AND datetime(timestamp) <= datetime(?)"
            app_params.append(end_time)
            cmd_query += (
                " AND datetime(c.timestamp) <= datetime(?)" if session_id
                else " AND datetime(timestamp) <= datetime(?)"
            )
            cmd_params.append(end_time)
            ev_query += " AND datetime(timestamp) <= datetime(?)"
            ev_params.append(end_time)

        req_query += " ORDER BY timestamp DESC LIMIT 50"
        app_query += " ORDER BY timestamp DESC LIMIT 50"
        cmd_query += (
            " ORDER BY c.timestamp DESC LIMIT 50" if session_id
            else " ORDER BY timestamp DESC LIMIT 50"
        )
        ev_query += " ORDER BY id ASC"

        requests = [dict(row) for row in conn.execute(req_query, req_params).fetchall()]
        approvals = [dict(row) for row in conn.execute(app_query, app_params).fetchall()]
        commands = [dict(row) for row in conn.execute(cmd_query, cmd_params).fetchall()]
        events = [dict(row) for row in conn.execute(ev_query, ev_params).fetchall()]

    return {
        "requests": requests,
        "approvals": approvals,
        "commands": commands,
        "events": events,
    }


def _message_content(message) -> str:
    if isinstance(message, str):
        return message
    return getattr(message, "content", "") or ""


def _message_tool_calls(message):
    return getattr(message, "tool_calls", None) or []


def _serialize_tool_calls(tool_calls):
    return [
        {
            "id": tool_call.id,
            "type": tool_call.type,
            "function": {
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
            },
        }
        for tool_call in tool_calls
    ]


def _approval_block(approval_id: str, tool_calls) -> str:
    tool_names = [tool_call["function"]["name"] for tool_call in tool_calls]
    return (
        "[APPROVAL_REQUIRED]\n"
        f"Tools: [{', '.join(tool_names)}]\n"
        f"Approve: jala approve {approval_id}\n"
        f"Deny: jala deny {approval_id}"
    )


def _extract_tool_command(tool_call) -> str:
    function = tool_call["function"]
    try:
        arguments = json.loads(function["arguments"])
    except (json.JSONDecodeError, TypeError) as error:
        logger.warning("Malformed tool call arguments: %s", error)
        return ""
    return arguments.get("command", "")


class ParseError(Exception):
    pass

def _tokenize_bash(script: str):
    tokens = []
    i = 0
    n = len(script)
    while i < n:
        c = script[i]
        if c.isspace():
            i += 1
            continue
        if c in '();<>|&':
            op = c
            if i + 1 < n:
                nc = script[i+1]
                if op + nc in ('||', '&&', '>>', '<<', '<&', '>&', ';;', '&>'):
                    op += nc
                    i += 1
            tokens.append(('OP', op))
            i += 1
            continue
            
        word = ""
        sub_commands = []
        
        while i < n:
            c = script[i]
            if c.isspace() or c in '();<>|&':
                break
                
            if c == '\\':
                word += c
                if i + 1 < n:
                    word += script[i+1]
                    i += 2
                else:
                    i += 1
            elif c == "'":
                word += c
                i += 1
                while i < n and script[i] != "'":
                    word += script[i]
                    i += 1
                if i < n:
                    word += script[i]
                    i += 1
                else:
                    raise ParseError("Unclosed single quote")
            elif c == '"':
                word += c
                i += 1
                while i < n and script[i] != '"':
                    if script[i] == '\\':
                        word += script[i]
                        if i + 1 < n:
                            word += script[i+1]
                            i += 2
                        else:
                            i += 1
                    elif script[i:i+2] == '$(':
                        word += '$('
                        sub_start = i + 2
                        i += 2
                        depth = 1
                        while i < n and depth > 0:
                            if script[i] == '\\':
                                word += script[i]
                                if i + 1 < n:
                                    word += script[i+1]
                                    i += 2
                                else:
                                    i += 1
                            elif script[i:i+2] == '$(':
                                depth += 1
                                word += '$('
                                i += 2
                            elif script[i] == '(':
                                depth += 1
                                word += '('
                                i += 1
                            elif script[i] == ')':
                                depth -= 1
                                word += ')'
                                i += 1
                            elif script[i] == "'":
                                word += "'"
                                i += 1
                                while i < n and script[i] != "'":
                                    word += script[i]
                                    i += 1
                                if i < n:
                                    word += "'"
                                    i += 1
                            else:
                                word += script[i]
                                i += 1
                        if depth > 0:
                            raise ParseError("Unclosed $('")
                        sub_commands.append(script[sub_start:i-1])
                    elif script[i] == '`':
                        word += '`'
                        sub_start = i + 1
                        i += 1
                        while i < n and script[i] != '`':
                            if script[i] == '\\':
                                word += script[i]
                                if i + 1 < n:
                                    word += script[i+1]
                                    i += 2
                                else:
                                    i += 1
                            else:
                                word += script[i]
                                i += 1
                        if i >= n:
                            raise ParseError("Unclosed backtick")
                        sub_commands.append(script[sub_start:i])
                        word += '`'
                        i += 1
                    else:
                        word += script[i]
                        i += 1
                if i < n:
                    word += '"'
                    i += 1
                else:
                    raise ParseError("Unclosed double quote")
            elif script[i:i+2] == '$(':
                word += '$('
                sub_start = i + 2
                i += 2
                depth = 1
                while i < n and depth > 0:
                    if script[i] == '\\':
                        word += script[i]
                        if i + 1 < n:
                            word += script[i+1]
                            i += 2
                        else:
                            i += 1
                    elif script[i:i+2] == '$(':
                        depth += 1
                        word += '$('
                        i += 2
                    elif script[i] == '(':
                        depth += 1
                        word += '('
                        i += 1
                    elif script[i] == ')':
                        depth -= 1
                        word += ')'
                        i += 1
                    elif script[i] == "'":
                        word += "'"
                        i += 1
                        while i < n and script[i] != "'":
                            word += script[i]
                            i += 1
                        if i < n:
                            word += "'"
                            i += 1
                    else:
                        word += script[i]
                        i += 1
                if depth > 0:
                    raise ParseError("Unclosed $('")
                sub_commands.append(script[sub_start:i-1])
            elif script[i] == '`':
                word += '`'
                sub_start = i + 1
                i += 1
                while i < n and script[i] != '`':
                    if script[i] == '\\':
                        word += script[i]
                        if i + 1 < n:
                            word += script[i+1]
                            i += 2
                        else:
                            i += 1
                    else:
                        word += script[i]
                        i += 1
                if i >= n:
                    raise ParseError("Unclosed backtick")
                sub_commands.append(script[sub_start:i])
                word += '`'
                i += 1
            else:
                word += c
                i += 1
                
        if word:
            tokens.append(('WORD', word, sub_commands))
            
    return tokens

def _unquote(word):
    try:
        return shlex.split(word)[0] if word else ""
    except Exception:
        return word

def _is_safe_command_sequence(tokens):
    current_command = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        kind = token[0]
        val = token[1]
        subs = token[2] if len(token) > 2 else []
        
        if kind == 'OP':
            if val in ('>', '>>', '&>', '>&', '<&', '<>', '<<', '<<-', '<<<'):
                return False
            if val == '<':
                if i + 1 < n and tokens[i+1][0] == 'WORD':
                    next_subs = tokens[i+1][2] if len(tokens[i+1]) > 2 else []
                    for sub in next_subs:
                        try:
                            sub_tokens = _tokenize_bash(sub)
                            if not _is_safe_command_sequence(sub_tokens):
                                return False
                        except ParseError:
                            return False
                    i += 2
                else:
                    i += 1
                continue
                
            if current_command:
                if not _is_safe_simple_command(current_command):
                    return False
                current_command = []
        elif kind == 'WORD':
            for sub in subs:
                try:
                    sub_tokens = _tokenize_bash(sub)
                    if not _is_safe_command_sequence(sub_tokens):
                        return False
                except ParseError:
                    return False
            current_command.append(val)
            
        i += 1
            
    if current_command:
        if not _is_safe_simple_command(current_command):
            return False
            
    return True

def _is_safe_simple_command(words):
    if not words:
        return True

    i = 0

    if '=' in words[i]:
        unq = _unquote(words[i])
        if '=' in unq and unq.split('=')[0].isidentifier():
            return False

    executable = _unquote(words[i])
    args = [_unquote(w) for w in words[i:]]

    if executable == 'find':
        if any(flag in UNSAFE_FIND_FLAGS for flag in args[1:]):
            return False
        return True

    if executable == 'git':
        if len(args) > 1 and args[1] in READ_ONLY_GIT_SUBCOMMANDS:
            return True
        return False

    if executable in ('sh', 'bash'):
        for j in range(1, len(args)):
            if args[j] == '-c':
                if j + 1 >= len(args):
                    return False
                inner_script = args[j + 1]
                try:
                    inner_tokens = _tokenize_bash(inner_script)
                    if not _is_safe_command_sequence(inner_tokens):
                        return False
                except ParseError:
                    return False
                return True
            elif args[j].startswith('-'):
                return False
        return False

    if executable == 'env':
        if len(args) == 1:
            return True
        for j in range(1, len(args)):
            if args[j].startswith('-') or '=' in args[j]:
                return False
            else:
                inner_words = words[i + j:]
                return _is_safe_simple_command(inner_words)
        return False

    return executable in READ_ONLY_COMMANDS

def _is_read_only_command(command: str) -> bool:
    """Classify a shell command string as read-only or mutating.

    Retained as a public helper for external callers and test coverage of the
    bash-safety parser.  It is no longer used by the runtime approval gate
    (``_requires_approval`` unconditionally gates every ``run_shell_command``
    call), but it remains available for downstream tooling that wants a
    best-effort read-only check on arbitrary shell strings.
    """
    try:
        tokens = _tokenize_bash(command)
        return _is_safe_command_sequence(tokens)
    except ParseError:
        return False

def _requires_approval(tool_call) -> bool:
    function = tool_call["function"]
    name = function["name"]
    if name in INSPECTION_TOOL_NAMES:
        return False
    return True

def _tool_result_content(output: str, exit_code: int) -> str:
    if not output:
        output = "(no output)"
    return f"Exit code: {exit_code}\nOutput:\n{output}"

def _tool_succeeded(result: str) -> bool:
    return result.startswith("Exit code: 0\n")

def _validate_tool_arg(arguments: dict, key: str, expected_type: type, *, allow_empty_str: bool = False):
    value = arguments.get(key)
    if value is None:
        return None, f"Error: required argument '{key}' is missing."
    if not isinstance(value, expected_type):
        return None, f"Error: invalid argument '{key}' — expected {expected_type.__name__}, got {type(value).__name__}."
    if expected_type is str and not allow_empty_str and not value.strip():
        return None, f"Error: invalid argument '{key}' — value must not be empty."
    return value, None


def _execute_read_only_tool_call(tool_call, cwd: str, terminal: NeutralTerminal | None = None):
    function = tool_call["function"]
    name = function["name"]

    try:
        arguments = json.loads(function["arguments"])
    except (json.JSONDecodeError, TypeError):
        return _tool_result_content("Error: invalid tool arguments — could not parse JSON.", 1)

    if not isinstance(arguments, dict):
        return _tool_result_content("Error: invalid tool arguments — expected a JSON object.", 1)

    if name == "read_file":
        path, err = _validate_tool_arg(arguments, "path", str)
        if err:
            return _tool_result_content(err, 1)
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        max_bytes = int(os.environ.get("JALA_MAX_FILE_READ_BYTES", str(DEFAULT_MAX_FILE_READ_BYTES)))
        try:
            file_size = os.path.getsize(full_path)
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_bytes)
            if file_size > max_bytes:
                content += f"\n[truncated: file is {file_size} bytes, showed first {max_bytes}]"
            return _tool_result_content(content, 0)
        except FileNotFoundError:
            return _tool_result_content(f"Error: file not found — {full_path}", 1)
        except PermissionError:
            return _tool_result_content(f"Error: permission denied — {full_path}", 1)
        except IsADirectoryError:
            return _tool_result_content(f"Error: path is a directory, not a file — {full_path}", 1)
        except OSError as exc:
            return _tool_result_content(f"Error: failed to read file — {exc}", 1)

    elif name == "search_files":
        pattern, err = _validate_tool_arg(arguments, "pattern", str)
        if err:
            return _tool_result_content(err, 1)
        directory = arguments.get("directory") or cwd
        if not isinstance(directory, str) or not directory.strip():
            directory = cwd
        full_dir = directory if os.path.isabs(directory) else os.path.join(cwd, directory)
        if not os.path.isdir(full_dir):
            return _tool_result_content(f"Error: directory not found — {full_dir}", 1)
        try:
            if terminal is None:
                terminal = NeutralTerminal()
            command = ["find", full_dir, "-name", pattern]
            output, exit_code = terminal.execute_local_args(command, cwd=cwd, timeout=30)
            return _tool_result_content(output, exit_code)
        except Exception as exc:
            return _tool_result_content(f"Error: search failed — {exc}", 1)

    elif name == "inspect_process":
        pid, err = _validate_tool_arg(arguments, "pid", int)
        if err:
            return _tool_result_content(err, 1)
        if pid <= 0:
            return _tool_result_content(f"Error: invalid pid — must be a positive integer, got {pid}.", 1)
        try:
            if terminal is None:
                terminal = NeutralTerminal()
            command = ["ps", "-p", str(pid), "-o", "user,pid,ppid,%cpu,%mem,vsz,rss,tty,stat,start,time,command"]
            output, exit_code = terminal.execute_local_args(command, cwd=cwd, timeout=30)
            return _tool_result_content(output, exit_code)
        except Exception as exc:
            return _tool_result_content(f"Error: process inspection failed — {exc}", 1)

    elif name == "list_directory":
        path = arguments.get("path") or cwd
        if not isinstance(path, str) or not path.strip():
            path = cwd
        show_hidden = arguments.get("show_hidden", False)
        if not isinstance(show_hidden, bool):
            show_hidden = False
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        full_path = os.path.realpath(full_path)
        if not os.path.isdir(full_path):
            return _tool_result_content(f"Error: directory not found — {full_path}", 1)
        try:
            entries = os.scandir(full_path)
            lines = []
            for entry in sorted(entries, key=lambda e: (not e.is_dir(), e.name.lower())):
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    size = st.st_size
                    mode = stat.filemode(st.st_mode)
                except OSError:
                    mode = "??????????"
                    size = -1
                kind = "d" if entry.is_dir(follow_symlinks=False) else ("l" if entry.is_symlink() else "f")
                size_str = f"{size:>10}" if size >= 0 else "         ?"
                lines.append(f"{mode}  {size_str}  {entry.name}{'/' if kind == 'd' else ''}")
            if not lines:
                output = f"(directory is empty: {full_path})"
            else:
                output = f"{full_path}:\n" + "\n".join(lines)
            return _tool_result_content(output, 0)
        except PermissionError:
            return _tool_result_content(f"Error: permission denied — {full_path}", 1)
        except OSError as exc:
            return _tool_result_content(f"Error: failed to list directory — {exc}", 1)

    elif name == "search_file_contents":
        pattern, err = _validate_tool_arg(arguments, "pattern", str)
        if err:
            return _tool_result_content(err, 1)
        search_path = arguments.get("path") or cwd
        if not isinstance(search_path, str) or not search_path.strip():
            search_path = cwd
        full_search_path = search_path if os.path.isabs(search_path) else os.path.join(cwd, search_path)
        full_search_path = os.path.realpath(full_search_path)
        if not os.path.exists(full_search_path):
            return _tool_result_content(f"Error: path not found — {full_search_path}", 1)
        glob_pattern = arguments.get("glob")
        if glob_pattern is not None and not isinstance(glob_pattern, str):
            glob_pattern = None
        ignore_case = arguments.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            ignore_case = False
        max_results = arguments.get("max_results", 100)
        if not isinstance(max_results, int) or max_results <= 0:
            max_results = 100
        max_results = min(max_results, 1000)
        max_file_bytes = int(os.environ.get("JALA_MAX_FILE_READ_BYTES", str(DEFAULT_MAX_FILE_READ_BYTES)))
        max_files = 2000
        try:
            flags = re.IGNORECASE if ignore_case else 0
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return _tool_result_content(f"Error: invalid regex pattern — {exc}", 1)
        matches = []
        files_to_search = []
        if os.path.isfile(full_search_path):
            files_to_search = [full_search_path]
        else:
            for dirpath, _dirnames, filenames in os.walk(full_search_path):
                for fname in filenames:
                    if glob_pattern and not fnmatch.fnmatch(fname, glob_pattern):
                        continue
                    files_to_search.append(os.path.join(dirpath, fname))
                    if len(files_to_search) >= max_files:
                        break
                if len(files_to_search) >= max_files:
                    break
        files_capped = len(files_to_search) >= max_files
        for fpath in files_to_search:
            if len(matches) >= max_results:
                break
            try:
                fsize = os.path.getsize(fpath)
                if fsize > max_file_bytes:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if len(matches) >= max_results:
                            break
                        if compiled.search(line):
                            rel = os.path.relpath(fpath, full_search_path) if os.path.isdir(full_search_path) else fpath
                            matches.append(f"{rel}:{lineno}: {line.rstrip()}")
            except (PermissionError, OSError):
                continue
        if not matches:
            output = f"No matches found for pattern: {pattern}"
        else:
            output = "\n".join(matches)
        notes = []
        if len(matches) >= max_results:
            notes.append(f"results capped at {max_results} — use max_results or a narrower path/glob to refine")
        if files_capped:
            notes.append(f"file walk capped at {max_files} files — use a narrower path or glob to search more")
        if notes:
            output += "\n[" + "; ".join(notes) + "]"
        return _tool_result_content(output, 0)

    elif name == "file_metadata":
        path, err = _validate_tool_arg(arguments, "path", str)
        if err:
            return _tool_result_content(err, 1)
        raw_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        try:
            lstat = os.lstat(raw_path)
            is_link = stat.S_ISLNK(lstat.st_mode)
            st = os.stat(raw_path)
            if stat.S_ISDIR(st.st_mode):
                kind = "directory"
            elif stat.S_ISREG(st.st_mode):
                kind = "file"
            elif is_link:
                kind = "symlink"
            else:
                kind = "other"
            mode_str = stat.filemode(lstat.st_mode)
            size = st.st_size
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            display_path = os.path.realpath(raw_path)
            lines = [
                f"path:         {display_path}",
                f"type:         {kind}",
                f"size:         {size} bytes",
                f"permissions:  {mode_str}",
                f"modified:     {mtime}",
            ]
            if is_link:
                try:
                    lines.append(f"link target:  {os.readlink(raw_path)}")
                except OSError:
                    pass
            output = "\n".join(lines)
            return _tool_result_content(output, 0)
        except FileNotFoundError:
            return _tool_result_content(f"Error: path not found — {raw_path}", 1)
        except PermissionError:
            return _tool_result_content(f"Error: permission denied — {raw_path}", 1)
        except OSError as exc:
            return _tool_result_content(f"Error: failed to read metadata — {exc}", 1)

    elif name == "list_processes":
        name_filter = arguments.get("name_filter")
        if name_filter is not None and not isinstance(name_filter, str):
            name_filter = None
        if name_filter is not None and not name_filter.strip():
            name_filter = None
        try:
            if terminal is None:
                terminal = NeutralTerminal()
            command = ["ps", "axo", "user,pid,ppid,%cpu,%mem,stat,start,time,command"]
            output, exit_code = terminal.execute_local_args(command, cwd=cwd, timeout=30)
            if exit_code != 0:
                return _tool_result_content(output, exit_code)
            if name_filter:
                lines = output.splitlines()
                header = lines[0] if lines else ""
                filtered = [l for l in lines[1:] if name_filter.lower() in l.lower()]
                if not filtered:
                    output = f"{header}\n(no processes matching '{name_filter}')"
                else:
                    output = "\n".join([header] + filtered)
            return _tool_result_content(output, 0)
        except Exception as exc:
            return _tool_result_content(f"Error: process listing failed — {exc}", 1)

    elif name == "git_inspect":
        subcommand, err = _validate_tool_arg(arguments, "subcommand", str)
        if err:
            return _tool_result_content(err, 1)
        subcommand = subcommand.strip()
        if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
            allowed = ", ".join(sorted(READ_ONLY_GIT_SUBCOMMANDS))
            return _tool_result_content(
                f"Error: unsupported git subcommand '{subcommand}'. Allowed: {allowed}.", 1
            )
        extra_args = arguments.get("args", [])
        if not isinstance(extra_args, list):
            extra_args = []
        # Validate each argument against the per-subcommand allowlist.
        # Non-flag arguments (refs, paths, commit hashes) are passed through.
        # Flag arguments must appear in the subcommand's allowed set and must
        # not appear in the always-blocked set.
        allowed_flags = _GIT_ALLOWED_FLAGS.get(subcommand, frozenset())
        validated_args = []
        for raw in extra_args:
            if not isinstance(raw, str):
                continue
            arg = raw.strip()
            if not arg:
                continue
            if arg.startswith("-"):
                # Normalise --flag=value to just --flag for allowlist lookup.
                flag_name = arg.split("=", 1)[0]
                if flag_name in _GIT_ALWAYS_BLOCKED_FLAGS:
                    return _tool_result_content(
                        f"Error: git flag '{flag_name}' is not permitted.", 1
                    )
                if flag_name not in allowed_flags:
                    return _tool_result_content(
                        f"Error: git flag '{flag_name}' is not permitted for 'git {subcommand}'. "
                        f"Allowed flags: {', '.join(sorted(allowed_flags)) or '(none)'}.", 1
                    )
            validated_args.append(arg)
        command = ["git", subcommand] + validated_args
        try:
            if terminal is None:
                terminal = NeutralTerminal()
            output, exit_code = terminal.execute_local_args(command, cwd=cwd, timeout=30)
            return _tool_result_content(output, exit_code)
        except Exception as exc:
            return _tool_result_content(f"Error: git inspection failed — {exc}", 1)

    return _tool_result_content(f"Error: unknown read-only tool '{name}'.", 1)

def process_chat(message: str, cwd: str, session_id: str) -> str:
    with _session_lock(session_id):
        return _process_chat_locked(message, cwd, session_id)

def _process_chat_locked(message: str, cwd: str, session_id: str) -> str:
    _record_request(session_id, message)
    history = _session_history(session_id)
    start_len = len(history)
    history.append({"role": "system", "content": f"Current working directory: {cwd}"})
    history.append({"role": "user", "content": message})

    terminal = NeutralTerminal()
    model_timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", str(DEFAULT_MODEL_TIMEOUT_SECONDS)))
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            reply_message = terminal.connect_to_chatgpt_messages(
                history,
                format="text",
                tools=TOOLS,
                timeout=model_timeout,
            )

            content = _message_content(reply_message)
            tool_calls = _message_tool_calls(reply_message)
            if not tool_calls:
                history.append({"role": "assistant", "content": content})
                _save_session_history(session_id, history)
                return content

            serialized_tool_calls = _serialize_tool_calls(tool_calls)
            assistant_message = {"role": "assistant", "tool_calls": serialized_tool_calls}
            if content:
                assistant_message["content"] = content
            history.append(assistant_message)

            inspection_calls = [tc for tc in serialized_tool_calls if not _requires_approval(tc)]
            approval_calls = [tc for tc in serialized_tool_calls if _requires_approval(tc)]

            for tool_call in inspection_calls:
                history.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": _execute_read_only_tool_call(tool_call, cwd, terminal),
                })

            if approval_calls:
                # Each mutating call gets its own approval record so that
                # every approval is atomic: approve-then-execute one command.
                # Batching multiple mutating calls into one approval would allow
                # partial execution if an intermediate command fails, which
                # violates the atomicity guarantee.
                blocks = []
                for tool_call in approval_calls:
                    approval_id = uuid.uuid4().hex[:16]
                    _save_approval(approval_id, session_id, cwd, [tool_call], status='pending')
                    history.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": tool_call["function"]["name"],
                        "content": "Action pending user approval.",
                    })
                    blocks.append(_approval_block(approval_id, [tool_call]))
                _save_session_history(session_id, history)
                combined_block = "\n\n".join(blocks)
                return f"{content.strip()}\n\n{combined_block}".strip() if content else combined_block
    except Exception:
        del history[start_len:]
        raise

    del history[start_len:]
    raise ToolLoopError("Maximum tool rounds exceeded.")


def _execute_tool_call(approval_id: str, tool_call, cwd: str):
    function = tool_call["function"]
    name = function["name"]
    try:
        arguments = json.loads(function["arguments"])
    except (json.JSONDecodeError, TypeError) as error:
        logger.warning("Malformed tool call arguments for approval %s: %s", approval_id, error)
        arguments = {}

    if name in INSPECTION_TOOL_NAMES:
        result = _execute_read_only_tool_call(tool_call, cwd)
        exit_code = 0 if _tool_succeeded(result) else 1
        _record_command_execution(approval_id, name, cwd, exit_code, 0.0, result)
        return result

    if name != "run_shell_command":
        err_msg = f"Error: unknown tool '{name}'."
        _record_command_execution(approval_id, name, cwd, 1, 0.0, err_msg)
        return err_msg

    command = arguments.get("command", "")
    terminal = NeutralTerminal()
    start_t = time.time()
    output, exit_code = terminal.execute_local(command, cwd=cwd, timeout=30)
    duration = time.time() - start_t
    _record_command_execution(approval_id, command, cwd, exit_code, duration, output)
    return _tool_result_content(output, exit_code)


def process_approval(approval_id: str, approved: bool) -> str:
    approval_info = _get_approval(approval_id)
    if approval_info is None:
        raise KeyError("Approval ID not found.")
    session_id = approval_info["session_id"]
    with _session_lock(session_id):
        return _process_approval_locked(approval_id, approved)

def _process_approval_locked(approval_id: str, approved: bool) -> str:
    approval_info = _get_approval(approval_id)
    if approval_info is None:
        raise KeyError("Approval ID not found.")
    if approval_info["status"] == "executing":
        if _command_execution_count(approval_id) == 0:
            if _update_approval_status(
                approval_id,
                "pending",
                expected_status="executing",
                error_text="Recovered interrupted execution before command start.",
            ):
                approval_info = _get_approval(approval_id) or approval_info
        else:
            _update_approval_status(
                approval_id,
                "failed",
                expected_status="executing",
                error_text="Execution interrupted after command start; refusing automatic retry.",
            )
            raise KeyError(
                "Approval execution was interrupted after command start and has been marked failed."
            )

    if approval_info["status"] != "pending":
        raise KeyError("Approval ID is not pending.")

    session_id = approval_info["session_id"]
    history = _session_history(session_id)
    cwd = approval_info["cwd"]

    pending_tool_call_ids = {tc["id"] for tc in approval_info["tool_calls"]}
    history = [
        msg for msg in history
        if not (
            msg.get("role") == "tool"
            and msg.get("tool_call_id") in pending_tool_call_ids
            and msg.get("content") == "Action pending user approval."
        )
    ]

    if approved:
        if not _update_approval_status(approval_id, "executing", expected_status="pending"):
            raise KeyError("Approval ID is not pending.")
        try:
            tool_calls = approval_info["tool_calls"]
            results = []
            failed_index = None
            for i, tool_call in enumerate(tool_calls):
                result = _execute_tool_call(approval_id, tool_call, cwd)
                history.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": result,
                })
                results.append((tool_call["function"]["name"], result))
                if not _tool_succeeded(result):
                    failed_index = i
                    break

            if failed_index is None:
                response = "Approved action executed."
                _update_approval_status(approval_id, "executed", expected_status="executing")
            else:
                failed_name, failed_output = results[failed_index]
                prior_count = failed_index
                if prior_count > 0:
                    prior_names = ", ".join(name for name, _ in results[:failed_index])
                    response = (
                        f"Approved action partially executed. "
                        f"{prior_count} earlier command(s) already ran ({prior_names}) and cannot be rolled back. "
                        f"Failed on '{failed_name}':\n{failed_output}"
                    )
                else:
                    response = f"Approved action failed.\n{failed_output}"
                _update_approval_status(
                    approval_id,
                    "failed",
                    expected_status="executing",
                    error_text=failed_output,
                )
        except Exception as error:
            logger.exception("Approval execution failed for %s", approval_id)
            _update_approval_status(
                approval_id,
                "failed",
                expected_status="executing",
                error_text=f"Approval execution error: {error}",
            )
            raise
    else:
        if not _update_approval_status(approval_id, "denied", expected_status="pending"):
            raise KeyError("Approval ID is not pending.")
        for tool_call in approval_info["tool_calls"]:
            history.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_call["function"]["name"],
                "content": "User denied the operation.",
            })
        response = "Denied approval request."

    history.append({"role": "assistant", "content": response})
    _save_session_history(session_id, history)
    return response


class APIHandler(BaseHTTPRequestHandler):
    def _check_auth(self) -> bool:
        expected = os.environ.get("API_AUTH_TOKEN")
        if not expected:
            return True
        provided = self.headers.get("Authorization", "")
        return hmac.compare_digest(provided, f"Bearer {expected}")

    def _send_json_error(self, status: int, message: str):
        self._send_json(status, {"error": message})

    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path in ("/", "/health"):
            if not self._check_auth():
                self._send_json_error(401, "Unauthorized")
                return
            self._send_json(200, {"status": "ok"})
            return
        if not self._check_auth():
            self._send_json_error(401, "Unauthorized")
            return
        if parsed_path.path == "/history":
            try:
                query = urllib.parse.parse_qs(parsed_path.query)
                session_id = query.get("session_id", [None])[0]
                event_type = query.get("event_type", [None])[0]
                start_time = query.get("start_time", [None])[0]
                end_time = query.get("end_time", [None])[0]

                history_data = _get_history_summary(session_id, event_type, start_time, end_time)
                self._send_json(200, {"history": history_data})
            except Exception:
                logger.exception("Failed to load /history payload")
                self._send_json_error(500, "Internal server error.")
            return
        self._send_json_error(404, "Not Found")

    def _read_payload(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            raise ValueError("Invalid Content-Length header")
        if content_length <= 0:
            raise ValueError("Empty body")
        max_request_bytes = int(os.environ.get("JALA_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)))
        if content_length > max_request_bytes:
            raise ValueError(f"Request body too large (max {max_request_bytes} bytes)")
        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Invalid JSON payload") from error

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if not self._check_auth():
            self._send_json_error(401, "Unauthorized")
            return

        if parsed_path.path == "/chat":
            try:
                payload = self._read_payload()
                message, cwd, session_id = _normalize_chat_request(payload)
                response = process_chat(message, cwd, session_id)
            except ValueError as error:
                self._send_json_error(400, str(error))
                return
            except StatePersistenceError:
                logger.exception("Persistence failure processing /chat request")
                self._send_json_error(500, "State persistence error. The daemon may be in a degraded state.")
                return
            except ToolLoopError as error:
                logger.warning("Tool loop exhausted processing /chat request: %s", error)
                self._send_json_error(500, str(error))
                return
            except Exception:
                logger.exception("Unexpected error processing /chat request")
                self._send_json_error(500, "Error communicating with AI.")
                return

            self._send_json(200, {"response": response})
            return

        if parsed_path.path in ("/approve", "/deny"):
            try:
                payload = self._read_payload()
                approval_id = _normalize_approval_request(payload)
                response = process_approval(
                    approval_id,
                    approved=(parsed_path.path == "/approve"),
                )
            except ValueError as error:
                self._send_json_error(400, str(error))
                return
            except KeyError as error:
                self._send_json_error(404, str(error))
                return
            except Exception:
                logger.exception("Failed to process %s request", parsed_path.path)
                self._send_json_error(500, "Error executing approval.")
                return

            self._send_json(200, {"response": response})
            return

        self._send_json_error(404, "Not Found")


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, max_workers: int):
        self._request_slots = threading.BoundedSemaphore(max(1, max_workers))
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address):
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _build_ssl_context() -> ssl.SSLContext | None:
    """Return an SSLContext if API_TLS_CERT and API_TLS_KEY are both set, else None."""
    cert = os.environ.get("API_TLS_CERT", "").strip()
    key = os.environ.get("API_TLS_KEY", "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise RuntimeError(
            "Both API_TLS_CERT and API_TLS_KEY must be set to enable TLS. "
            "Set one or the other is not valid."
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(certfile=cert, keyfile=key)
    except (ssl.SSLError, OSError) as exc:
        raise RuntimeError(f"Failed to load TLS certificate/key: {exc}") from exc
    return ctx


def run_server():
    load_environment()
    load_state()
    host = os.environ.get("API_HOST", DEFAULT_API_HOST)
    port = int(os.environ.get("API_PORT", str(DEFAULT_API_PORT)))
    auth_token = os.environ.get("API_AUTH_TOKEN")
    max_workers = int(
        os.environ.get("JALA_MAX_CONCURRENT_REQUESTS", str(DEFAULT_MAX_CONCURRENT_REQUESTS))
    )
    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    if not is_loopback and not auth_token:
        raise RuntimeError("API_AUTH_TOKEN is required when binding the daemon to a non-loopback host.")

    ssl_ctx = _build_ssl_context()
    scheme = "https" if ssl_ctx else "http"

    if not is_loopback and not ssl_ctx:
        print(
            "WARNING: daemon is bound to a non-loopback host without TLS. "
            "All traffic including the API_AUTH_TOKEN bearer credential is sent in cleartext. "
            "Set API_TLS_CERT and API_TLS_KEY to enable TLS, or use a TLS-terminating reverse proxy.",
            file=sys.stderr,
        )

    server = LimitedThreadingHTTPServer((host, port), APIHandler, max_workers=max_workers)
    if ssl_ctx:
        server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)

    print(f"Starting API Server on {scheme}://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

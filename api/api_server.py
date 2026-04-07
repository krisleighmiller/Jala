import hmac
import json
import logging
import os
import shlex
import sqlite3
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
    "follow-up questions. Prefer the registered structured tools (e.g., read_file, search_files, inspect_process) "
    "for read-only inspection tasks. Reserve the run_shell_command tool for cases "
    "where no structured tool can satisfy the request or you need to modify the user's "
    "local environment. State-changing shell commands require explicit user approval."
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
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read.",
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
            "description": "Search for files by name or pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The search pattern.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "The directory to search in. Defaults to cwd.",
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
            "description": "Inspect running processes.",
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
    }
]

MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 200
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_MODEL_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_MAX_CONCURRENT_REQUESTS = 16
DEFAULT_MAX_FILE_READ_BYTES = 256 * 1024

INSPECTION_TOOL_NAMES = frozenset({"read_file", "search_files", "inspect_process"})
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
    conn = sqlite3.connect(_state_db_path())
    conn.row_factory = sqlite3.Row
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
        output = "(Command executed successfully with no output)"
    return f"Exit code: {exit_code}\nOutput:\n{output}"

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
                approval_id = uuid.uuid4().hex[:16]
                _save_approval(approval_id, session_id, cwd, approval_calls, status='pending')
                for tool_call in approval_calls:
                    history.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": tool_call["function"]["name"],
                        "content": "Action pending user approval.",
                    })
                _save_session_history(session_id, history)
                block = _approval_block(approval_id, approval_calls)
                return f"{content.strip()}\n\n{block}".strip() if content else block
    except Exception:
        del history[start_len:]
        raise

    del history[start_len:]
    raise RuntimeError("Maximum tool rounds exceeded.")


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
        exit_code = 0 if result.startswith("Exit code: 0\n") else 1
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
            all_success = True
            failed_output = None
            for tool_call in approval_info["tool_calls"]:
                result = _execute_tool_call(approval_id, tool_call, cwd)
                history.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": result,
                })
                if not result.startswith("Exit code: 0\n"):
                    all_success = False
                    failed_output = result
                    break

            if all_success:
                response = "Approved action executed."
                _update_approval_status(approval_id, "executed", expected_status="executing")
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
            except Exception:
                logger.exception("Failed to process /chat request")
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


def run_server():
    load_environment()
    load_state()
    host = os.environ.get("API_HOST", DEFAULT_API_HOST)
    port = int(os.environ.get("API_PORT", str(DEFAULT_API_PORT)))
    auth_token = os.environ.get("API_AUTH_TOKEN")
    max_workers = int(
        os.environ.get("JALA_MAX_CONCURRENT_REQUESTS", str(DEFAULT_MAX_CONCURRENT_REQUESTS))
    )
    if host not in ("127.0.0.1", "localhost", "::1") and not auth_token:
        raise RuntimeError("API_AUTH_TOKEN is required when binding the daemon to a non-loopback host.")
    server = LimitedThreadingHTTPServer((host, port), APIHandler, max_workers=max_workers)
    print(f"Starting API Server on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

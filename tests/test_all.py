import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from api.api_server import (
    _get_db_connection,
    _session_history,
    _save_session_history,
    _get_approval,
    _save_approval,
    load_state,
    SYSTEM_PROMPT,
    StatePersistenceError,
    ToolLoopError,
    process_approval,
    run_server,
)
from core.neutral_terminal import NeutralTerminal


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _find_last_value(messages, role):
    for message in reversed(messages):
        if message["role"] == role:
            return message["content"]
    return ""


def _find_last_cwd(messages):
    for message in reversed(messages):
        content = message["content"]
        marker = "Current working directory: "
        if content.startswith(marker):
            return content[len(marker):]
    return ""


def _find_last_token(messages):
    marker = "Remember this token exactly: "
    for message in messages:
        content = message["content"]
        if marker in content:
            return content.split(marker, 1)[1].split(".", 1)[0].strip()
    return ""


class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


def _fake_tool_call_message(command):
    if command == "pwd":
        return types.SimpleNamespace(
            content="",
            tool_calls=[
                types.SimpleNamespace(
                    id="call_readonly",
                    type="function",
                    function=types.SimpleNamespace(
                        name="inspect_process",
                        arguments=json.dumps({"pid": 1}),
                    ),
                )
            ],
        )
    return types.SimpleNamespace(
        content="",
        tool_calls=[
            types.SimpleNamespace(
                id="call_readonly",
                type="function",
                function=types.SimpleNamespace(
                    name="run_shell_command",
                    arguments=json.dumps({"command": command}),
                ),
            )
        ],
    )

def _fake_chat(self, messages, model=None, max_tokens=None, temperature=None, format="json_object", timeout=None, tools=None):
    last_user = _find_last_value(messages, "user")
    last_assistant = _find_last_value(messages, "assistant").strip()
    current_cwd = _find_last_cwd(messages)
    lowered = last_user.lower()

    if "pick a one-word codename" in lowered:
        return FakeMessage("ALPHA")
    if "repeat the codename you just gave me" in lowered:
        return FakeMessage(last_assistant or "MISSING_ASSISTANT")
    if "remember this token exactly:" in lowered and "only ready" in lowered:
        return FakeMessage("READY")
    if "what token did i ask you to remember" in lowered:
        return FakeMessage(_find_last_token(messages) or "MISSING_TOKEN")
    if last_user.strip() == "pwd":
        return FakeMessage(current_cwd or "MISSING_CWD")
    return FakeMessage(f"ACK: {last_user}")

def _failing_chat(self, messages, model=None, max_tokens=None, temperature=None, format="json_object", timeout=None, tools=None):
    raise RuntimeError("simulated backend failure")


def _readonly_tool_chat(self, messages, model=None, max_tokens=None, temperature=None, format="json_object", timeout=None, tools=None):
    tool_messages = [message for message in messages if message["role"] == "tool"]
    if not tool_messages:
        return _fake_tool_call_message("pwd")

    latest_output = tool_messages[-1]["content"].split("Output:\n", 1)[1].strip()
    return FakeMessage(latest_output)


def _looping_tool_chat(self, messages, model=None, max_tokens=None, temperature=None, format="json_object", timeout=None, tools=None):
    return _fake_tool_call_message("pwd")


@pytest.fixture
def daemon_server(monkeypatch, tmp_path):
    state_dir = str(tmp_path / "jala_state")
    os.makedirs(state_dir, exist_ok=True)
    monkeypatch.setenv("JALA_STATE_DIR", state_dir)
    port = _free_port()
    monkeypatch.setenv("API_PORT", str(port))
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setattr(NeutralTerminal, "connect_to_chatgpt_messages", _fake_chat)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    for _ in range(40):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)

    yield f"127.0.0.1:{port}"


def test_daemon_health(daemon_server):
    req = urllib.request.Request(f"http://{daemon_server}/health")
    with urllib.request.urlopen(req) as response:
        assert response.status == 200
        data = json.loads(response.read().decode("utf-8"))
        assert data["status"] == "ok"


def test_cli_help():
    jala_path = os.path.abspath("jala")
    res = subprocess.run([sys.executable, jala_path, "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Usage: jala [chat] [-s|--session <session_id>] <message>" in res.stdout or "Usage: jala [-s|--session <session_id>] <message>" in res.stdout


def test_module_entrypoint_help():
    res = subprocess.run([sys.executable, "-m", "core", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Usage: jala [chat] [-s|--session <session_id>] <message>" in res.stdout or "Usage: jala [-s|--session <session_id>] <message>" in res.stdout


def test_empty_message():
    jala_path = os.path.abspath("jala")
    res = subprocess.run([sys.executable, jala_path, ""], capture_output=True, text=True)
    assert res.returncode != 0
    assert "Error: Message cannot be empty." in res.stderr


def test_interactive_flag_is_rejected():
    jala_path = os.path.abspath("jala")
    res = subprocess.run([sys.executable, jala_path, "--interactive"], capture_output=True, text=True)
    assert res.returncode != 0
    assert "error: unknown option: --interactive" in res.stderr.lower()


def test_cli_fails_without_daemon():
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = str(_free_port())
    jala_path = os.path.abspath("jala")
    res = subprocess.run([sys.executable, jala_path, "hello"], capture_output=True, text=True, env=env)
    assert res.returncode != 0
    assert "Error connecting to jala-daemon" in res.stderr


def test_session_creation_and_continuity(daemon_server):
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    cwd1 = os.getcwd()
    res1 = subprocess.run([sys.executable, jala_path, "Remember this token exactly: BANANA-42. Reply with ONLY READY."], capture_output=True, text=True, env=env, cwd=cwd1)
    assert res1.returncode == 0
    assert "READY" in res1.stdout

    cwd2 = tempfile.gettempdir()
    res2 = subprocess.run([sys.executable, jala_path, "What token did I ask you to remember? Reply with ONLY the token."], capture_output=True, text=True, env=env, cwd=cwd2)
    assert res2.returncode == 0
    assert "BANANA-42" in res2.stdout


def test_working_directory_is_sent_each_turn(daemon_server, tmp_path):
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    first_dir = str(tmp_path / "first")
    second_dir = str(tmp_path / "second")
    os.makedirs(first_dir)
    os.makedirs(second_dir)

    res1 = subprocess.run([sys.executable, jala_path, "pwd"], capture_output=True, text=True, env=env, cwd=first_dir)
    res2 = subprocess.run([sys.executable, jala_path, "pwd"], capture_output=True, text=True, env=env, cwd=second_dir)

    assert res1.returncode == 0
    assert res2.returncode == 0
    assert first_dir in res1.stdout
    assert second_dir in res2.stdout


def test_chat_endpoint_rejects_blank_message(daemon_server):
    req = urllib.request.Request(
        f"http://{daemon_server}/chat",
        data=json.dumps({"message": "   ", "cwd": os.getcwd(), "session_id": "default"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 400
    data = json.loads(excinfo.value.read().decode("utf-8"))
    assert data["error"] == "Missing 'message'"


def test_chat_endpoint_rejects_invalid_cwd(daemon_server):
    req = urllib.request.Request(
        f"http://{daemon_server}/chat",
        data=json.dumps({"message": "hello", "cwd": "/definitely/not/a/real/path", "session_id": "default"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 400
    data = json.loads(excinfo.value.read().decode("utf-8"))
    assert data["error"] == "Invalid 'cwd'"


def test_failed_chat_does_not_pollute_session(monkeypatch, daemon_server):
    monkeypatch.setattr(NeutralTerminal, "connect_to_chatgpt_messages", _failing_chat)

    req = urllib.request.Request(
        f"http://{daemon_server}/chat",
        data=json.dumps({"message": "hello", "cwd": os.getcwd(), "session_id": "rollback"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 500
    data = json.loads(excinfo.value.read().decode("utf-8"))
    assert data["error"] == "Error communicating with AI."
    assert len(_session_history("rollback")) == 1 # Just the SYSTEM_PROMPT


def test_readonly_tool_calls_stay_in_one_command_flow(monkeypatch, daemon_server):
    monkeypatch.setattr(NeutralTerminal, "connect_to_chatgpt_messages", _readonly_tool_chat)
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    request_dir = tempfile.mkdtemp()
    res = subprocess.run([sys.executable, jala_path, "pwd"], capture_output=True, text=True, env=env, cwd=request_dir)

    assert res.returncode == 0
    assert res.stdout.strip()
    assert "[APPROVAL_REQUIRED]" not in res.stdout

def test_save_state_failures_return_500_and_rollback(monkeypatch, daemon_server):
    def _failing_save_session_history(session_id, history):
        raise StatePersistenceError("simulated persistence failure")

    monkeypatch.setattr("api.api_server._save_session_history", _failing_save_session_history)

    req = urllib.request.Request(
        f"http://{daemon_server}/chat",
        data=json.dumps({"message": "hello", "cwd": os.getcwd(), "session_id": "persist-fail"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 500
    assert len(_session_history("persist-fail")) == 1


def test_max_tool_rounds_returns_500_and_rolls_back(monkeypatch, daemon_server):
    monkeypatch.setattr(NeutralTerminal, "connect_to_chatgpt_messages", _looping_tool_chat)

    req = urllib.request.Request(
        f"http://{daemon_server}/chat",
        data=json.dumps({"message": "loop forever", "cwd": os.getcwd(), "session_id": "looping"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 500
    data = json.loads(excinfo.value.read().decode("utf-8"))
    assert "Maximum tool rounds exceeded" in data["error"]
    assert len(_session_history("looping")) == 1


def test_approval_status_transitions(daemon_server):
    approval_id = "approve-status-1"
    _save_session_history("approval-order", [{"role": "system", "content": SYSTEM_PROMPT}])
    _save_approval(
        approval_id, 
        session_id="approval-order", 
        cwd="/tmp", 
        tool_calls=[
            {
                "id": "tool-1",
                "type": "function",
                "function": {
                    "name": "run_shell_command",
                    "arguments": json.dumps({"command": "touch created.txt"}),
                },
            }
        ],
        status='pending'
    )
    
    # execute approval
    response = process_approval(approval_id, approved=True)
    assert response == "Approved action executed."
    
    app = _get_approval(approval_id)
    assert app["status"] == "executed"
    
    # query history to check command execution
    req = urllib.request.Request(f"http://{daemon_server}/history")
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        cmds = data["history"]["commands"]
        assert any(cmd["approval_id"] == approval_id for cmd in cmds)

def test_failed_command_execution(daemon_server):
    approval_id = "approve-fail-1"
    _save_session_history("fail-order", [{"role": "system", "content": SYSTEM_PROMPT}])
    _save_approval(
        approval_id, 
        session_id="fail-order", 
        cwd="/tmp", 
        tool_calls=[
            {
                "id": "tool-1",
                "type": "function",
                "function": {
                    "name": "run_shell_command",
                    "arguments": json.dumps({"command": "exit 1"}),
                },
            }
        ],
        status='pending'
    )
    
    response = process_approval(approval_id, approved=True)
    assert "Approved action failed" in response
    
    app = _get_approval(approval_id)
    assert app["status"] == "failed"


def test_approval_is_marked_executing_before_command_completion(monkeypatch, daemon_server):
    approval_id = "approve-executing-1"
    _save_session_history("executing-order", [{"role": "system", "content": SYSTEM_PROMPT}])
    _save_approval(
        approval_id,
        session_id="executing-order",
        cwd="/tmp",
        tool_calls=[
            {
                "id": "tool-1",
                "type": "function",
                "function": {
                    "name": "run_shell_command",
                    "arguments": json.dumps({"command": "true"}),
                },
            }
        ],
        status='pending'
    )

    real_update = __import__("api.api_server", fromlist=["_update_approval_status"])._update_approval_status

    def _flaky_update(approval_id, status, **kwargs):
        if status == "executed":
            raise StatePersistenceError("final approval update failed")
        return real_update(approval_id, status, **kwargs)

    monkeypatch.setattr("api.api_server._update_approval_status", _flaky_update)

    with pytest.raises(StatePersistenceError):
        process_approval(approval_id, approved=True)

    app = _get_approval(approval_id)
    assert app["status"] == "failed"


def test_large_command_output_is_truncated(monkeypatch):
    monkeypatch.setenv("JALA_MAX_OUTPUT_BYTES", "64")
    terminal = NeutralTerminal()
    output, exit_code = terminal.execute_local_args(
        [sys.executable, "-c", "print('x' * 4096)"],
        timeout=5,
    )
    assert exit_code == 0
    assert "[output truncated: discarded " in output


def test_history_internal_errors_are_not_leaked(monkeypatch, daemon_server):
    monkeypatch.setattr("api.api_server._get_history_summary", lambda: (_ for _ in ()).throw(RuntimeError("secret-debug-detail")))
    req = urllib.request.Request(f"http://{daemon_server}/history")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 500
    data = json.loads(excinfo.value.read().decode("utf-8"))
    assert data["error"] == "Internal server error."

def test_history_cli_command(daemon_server):
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    res = subprocess.run([sys.executable, jala_path, "history"], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "=== Recent Requests ===" in res.stdout
    assert "=== Recent Approvals ===" in res.stdout
    assert "=== Recent Commands ===" in res.stdout


def test_history_requires_auth_token_when_configured(monkeypatch, daemon_server):
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")

    req = urllib.request.Request(f"http://{daemon_server}/history")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)
    assert excinfo.value.code == 401

    req = urllib.request.Request(
        f"http://{daemon_server}/history",
        headers={"Authorization": "Bearer secret-token"},
    )
    with urllib.request.urlopen(req) as response:
        assert response.status == 200


def test_cli_sends_auth_token_for_history(monkeypatch, daemon_server):
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    env["API_AUTH_TOKEN"] = "secret-token"
    jala_path = os.path.abspath("jala")

    res = subprocess.run([sys.executable, jala_path, "history"], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "=== Recent Requests ===" in res.stdout


def test_request_body_too_large_is_rejected(monkeypatch, daemon_server):
    monkeypatch.setenv("JALA_MAX_REQUEST_BYTES", "32")
    payload = json.dumps({"message": "hello", "cwd": os.getcwd(), "session_id": "default"}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{daemon_server}/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)

    assert excinfo.value.code == 400
    data = json.loads(excinfo.value.read().decode("utf-8"))
    assert "Request body too large" in data["error"]

def test_dotenv_file_is_loaded_for_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".env").write_text("OPENAI_API_KEY=dotenv-test-key\n", encoding="utf-8")
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        env["PYTHONPATH"] = os.getcwd()
        res = subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.neutral_terminal import NeutralTerminal; print(NeutralTerminal().api_key or '')",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
        )
        assert res.returncode == 0
        assert res.stdout.strip() == "dotenv-test-key"

def test_improved_shell_safety_boundary():
    from api.api_server import _is_read_only_command
    
    # clearly safe inspection commands
    assert _is_read_only_command("pwd") == True
    assert _is_read_only_command("ls -la /tmp") == True
    assert _is_read_only_command("grep 'foo' < bar.txt") == True
    assert _is_read_only_command("cat foo | grep bar") == True
    assert _is_read_only_command("echo $(pwd)") == True
    assert _is_read_only_command("ls $(echo /tmp)") == True
    
    # clearly mutating commands
    assert _is_read_only_command("rm -rf /") == False
    assert _is_read_only_command("ls > foo.txt") == False
    assert _is_read_only_command("echo hello >> foo.txt") == False
    assert _is_read_only_command("git commit -m 'foo'") == False
    
    # environment-changing wrapper forms
    assert _is_read_only_command("VAR=1 ls") == False
    assert _is_read_only_command("env VAR=1 ls") == False
    assert _is_read_only_command("env -i ls") == False
    assert _is_read_only_command("env --chdir /tmp ls") == False
    assert _is_read_only_command("sh -c 'rm -rf /'") == False
    assert _is_read_only_command("bash -c 'echo hello > file'") == False
    
    # ambiguous wrapper forms
    assert _is_read_only_command("sh -i -c 'ls'") == False
    assert _is_read_only_command("bash ls") == False
    
    # borderline shell-syntax cases
    assert _is_read_only_command("bash -c 'ls'") == True
    assert _is_read_only_command("sh -c 'ls | grep foo'") == True
    assert _is_read_only_command('echo "$(rm -rf /)"') == False
    assert _is_read_only_command("echo '`rm -rf /`'") == True # Single quotes do not execute
    assert _is_read_only_command("echo `rm -rf /`") == False
    assert _is_read_only_command("env ls") == True


def test_readonly_tool_calls_use_argv_execution(monkeypatch):
    from api.api_server import _execute_read_only_tool_call

    tool_call = {
        "id": "tool-1",
        "type": "function",
        "function": {
            "name": "inspect_process",
            "arguments": json.dumps({"pid": 1}),
        },
    }

    def _unexpected_shell(*args, **kwargs):
        raise AssertionError("shell execution should not be used for read-only commands")

    monkeypatch.setattr(NeutralTerminal, "execute_local", _unexpected_shell)
    monkeypatch.setattr(NeutralTerminal, "execute_local_args", lambda self, args, cwd=None, timeout=None: (cwd, 0))

    result = _execute_read_only_tool_call(tool_call, "/tmp")
    assert "Exit code: 0" in result
    assert "/tmp" in result


def test_session_history_events_are_append_only(monkeypatch, tmp_path):
    state_dir = tmp_path / "append_only_state"
    state_dir.mkdir()
    monkeypatch.setenv("JALA_STATE_DIR", str(state_dir))
    load_state()

    pending_history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "run it"},
        {
            "role": "tool",
            "tool_call_id": "tool-1",
            "name": "run_shell_command",
            "content": "Action pending user approval.",
        },
    ]
    executed_history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "run it"},
        {
            "role": "tool",
            "tool_call_id": "tool-1",
            "name": "run_shell_command",
            "content": "Exit code: 0\nOutput:\nok",
        },
    ]

    _save_session_history("append-only", pending_history)
    with _get_db_connection() as conn:
        initial_rows = conn.execute(
            "SELECT id, event_type FROM session_events WHERE session_id = ? ORDER BY id ASC",
            ("append-only",),
        ).fetchall()

    _save_session_history("append-only", executed_history)
    with _get_db_connection() as conn:
        final_rows = conn.execute(
            "SELECT id, event_type FROM session_events WHERE session_id = ? ORDER BY id ASC",
            ("append-only",),
        ).fetchall()

    assert [row["id"] for row in final_rows[: len(initial_rows)]] == [row["id"] for row in initial_rows]
    assert any(row["event_type"] == "_history_remove_last" for row in final_rows)
    assert _session_history("append-only") == executed_history


def test_corrupt_session_events_are_logged_and_skipped(monkeypatch, tmp_path, caplog):
    state_dir = tmp_path / "corrupt_state"
    state_dir.mkdir()
    monkeypatch.setenv("JALA_STATE_DIR", str(state_dir))
    load_state()

    with _get_db_connection() as conn:
        conn.execute(
            "INSERT INTO session_events (session_id, event_type, event_data) VALUES (?, ?, ?)",
            ("corrupt-session", "user", json.dumps({"role": "user", "content": "hello"})),
        )
        conn.execute(
            "INSERT INTO session_events (session_id, event_type, event_data) VALUES (?, ?, ?)",
            ("corrupt-session", "assistant", "{not valid json"),
        )

    caplog.clear()
    with caplog.at_level("WARNING", logger="jala.server"):
        history = _session_history("corrupt-session")

    assert history == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "hello"},
    ]
    assert any("Skipping corrupt session event" in record.message for record in caplog.records)


def test_history_cli_shows_matching_events(daemon_server):
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    seed = subprocess.run([sys.executable, jala_path, "hello history"], capture_output=True, text=True, env=env)
    assert seed.returncode == 0

    res = subprocess.run(
        [sys.executable, jala_path, "history", "--session_id", "default", "--event_type", "user"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "=== Matching Events ===" in res.stdout
    assert "Type: user" in res.stdout


def test_history_cli_rejects_unrecognized_args(daemon_server):
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    res = subprocess.run(
        [sys.executable, jala_path, "history", "some", "stray", "words"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "unrecognized arguments" in res.stderr


def test_health_endpoint_requires_auth_when_configured(monkeypatch, daemon_server):
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")

    req = urllib.request.Request(f"http://{daemon_server}/health")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)
    assert excinfo.value.code == 401

    req = urllib.request.Request(
        f"http://{daemon_server}/health",
        headers={"Authorization": "Bearer secret-token"},
    )
    with urllib.request.urlopen(req) as response:
        assert response.status == 200


def test_each_mutating_call_gets_its_own_approval(monkeypatch, daemon_server):
    """The chat flow must split multiple mutating tool calls into individual approvals
    so that every approval is atomic (one command per approval record).
    Batching them would risk partial execution if any command fails."""

    def _two_shell_calls(self, messages, model=None, max_tokens=None, temperature=None,
                         format="json_object", timeout=None, tools=None):
        tool_messages = [m for m in messages if m["role"] == "tool"]
        if tool_messages:
            return FakeMessage("Both commands pending.")
        return types.SimpleNamespace(
            content="",
            tool_calls=[
                types.SimpleNamespace(
                    id="call-a",
                    type="function",
                    function=types.SimpleNamespace(
                        name="run_shell_command",
                        arguments=json.dumps({"command": "echo first"}),
                    ),
                ),
                types.SimpleNamespace(
                    id="call-b",
                    type="function",
                    function=types.SimpleNamespace(
                        name="run_shell_command",
                        arguments=json.dumps({"command": "echo second"}),
                    ),
                ),
            ],
        )

    monkeypatch.setattr(NeutralTerminal, "connect_to_chatgpt_messages", _two_shell_calls)
    env = os.environ.copy()
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = daemon_server.rsplit(":", 1)[1]
    jala_path = os.path.abspath("jala")

    res = subprocess.run(
        [sys.executable, jala_path, "run two commands"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0
    # Both commands must produce separate approval blocks
    assert res.stdout.count("[APPROVAL_REQUIRED]") == 2


def test_tool_succeeded_helper():
    from api.api_server import _tool_result_content, _tool_succeeded
    assert _tool_succeeded(_tool_result_content("ok", 0)) is True
    assert _tool_succeeded(_tool_result_content("fail", 1)) is False
    assert _tool_succeeded(_tool_result_content("", 0)) is True
    assert _tool_succeeded(_tool_result_content("", 1)) is False


def test_search_file_contents_structured_tool(tmp_path, monkeypatch):
    from api.api_server import _execute_read_only_tool_call, _tool_succeeded

    (tmp_path / "a.py").write_text("import os\nprint('hello')\n")
    (tmp_path / "b.txt").write_text("some random text\nhello world\n")

    tool_call = {
        "id": "tc-1",
        "type": "function",
        "function": {
            "name": "search_file_contents",
            "arguments": json.dumps({"pattern": "hello", "path": str(tmp_path)}),
        },
    }
    result = _execute_read_only_tool_call(tool_call, str(tmp_path))
    assert _tool_succeeded(result)
    assert "hello" in result

    tool_call_glob = {
        "id": "tc-2",
        "type": "function",
        "function": {
            "name": "search_file_contents",
            "arguments": json.dumps({"pattern": "hello", "path": str(tmp_path), "glob": "*.py"}),
        },
    }
    result_glob = _execute_read_only_tool_call(tool_call_glob, str(tmp_path))
    assert _tool_succeeded(result_glob)
    assert "a.py" in result_glob

    tool_call_bad = {
        "id": "tc-3",
        "type": "function",
        "function": {
            "name": "search_file_contents",
            "arguments": json.dumps({"pattern": "[invalid(regex"}),
        },
    }
    result_bad = _execute_read_only_tool_call(tool_call_bad, str(tmp_path))
    assert not _tool_succeeded(result_bad)
    assert "invalid regex pattern" in result_bad


def test_list_directory_structured_tool(tmp_path):
    from api.api_server import _execute_read_only_tool_call, _tool_succeeded

    (tmp_path / "visible.txt").write_text("x")
    (tmp_path / ".hidden").write_text("y")
    (tmp_path / "subdir").mkdir()

    tool_call = {
        "id": "tc-1",
        "type": "function",
        "function": {
            "name": "list_directory",
            "arguments": json.dumps({"path": str(tmp_path)}),
        },
    }
    result = _execute_read_only_tool_call(tool_call, str(tmp_path))
    assert _tool_succeeded(result)
    assert "visible.txt" in result
    assert ".hidden" not in result
    assert "subdir/" in result

    tool_call_hidden = {
        "id": "tc-2",
        "type": "function",
        "function": {
            "name": "list_directory",
            "arguments": json.dumps({"path": str(tmp_path), "show_hidden": True}),
        },
    }
    result_hidden = _execute_read_only_tool_call(tool_call_hidden, str(tmp_path))
    assert _tool_succeeded(result_hidden)
    assert ".hidden" in result_hidden


def test_file_metadata_structured_tool(tmp_path):
    from api.api_server import _execute_read_only_tool_call, _tool_succeeded

    f = tmp_path / "sample.txt"
    f.write_text("hello")

    tool_call = {
        "id": "tc-1",
        "type": "function",
        "function": {
            "name": "file_metadata",
            "arguments": json.dumps({"path": str(f)}),
        },
    }
    result = _execute_read_only_tool_call(tool_call, str(tmp_path))
    assert _tool_succeeded(result)
    assert "type:         file" in result
    assert "size:         5 bytes" in result
    assert "permissions:" in result

    tool_call_missing = {
        "id": "tc-2",
        "type": "function",
        "function": {
            "name": "file_metadata",
            "arguments": json.dumps({"path": str(tmp_path / "nonexistent")}),
        },
    }
    result_missing = _execute_read_only_tool_call(tool_call_missing, str(tmp_path))
    assert not _tool_succeeded(result_missing)
    assert "not found" in result_missing


def test_git_inspect_structured_tool(tmp_path):
    from api.api_server import _execute_read_only_tool_call, _tool_succeeded

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)

    tool_call = {
        "id": "tc-1",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "status"}),
        },
    }
    result = _execute_read_only_tool_call(tool_call, str(tmp_path))
    assert _tool_succeeded(result)

    tool_call_bad = {
        "id": "tc-2",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "commit"}),
        },
    }
    result_bad = _execute_read_only_tool_call(tool_call_bad, str(tmp_path))
    assert not _tool_succeeded(result_bad)
    assert "unsupported git subcommand" in result_bad


def test_git_inspect_flag_allowlist(tmp_path):
    from api.api_server import _execute_read_only_tool_call, _tool_succeeded

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)

    # Permitted flag passes through (status works on an empty repo)
    tool_call_ok = {
        "id": "tc-1",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "status", "args": ["--short"]}),
        },
    }
    result_ok = _execute_read_only_tool_call(tool_call_ok, str(tmp_path))
    assert _tool_succeeded(result_ok)

    # Always-blocked flag is rejected regardless of subcommand
    tool_call_blocked = {
        "id": "tc-2",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "status", "args": ["--output=/tmp/stolen"]}),
        },
    }
    result_blocked = _execute_read_only_tool_call(tool_call_blocked, str(tmp_path))
    assert not _tool_succeeded(result_blocked)
    assert "not permitted" in result_blocked

    # Flag not in the subcommand's allowlist is rejected
    tool_call_unknown = {
        "id": "tc-3",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "status", "args": ["--unknown-flag"]}),
        },
    }
    result_unknown = _execute_read_only_tool_call(tool_call_unknown, str(tmp_path))
    assert not _tool_succeeded(result_unknown)
    assert "not permitted" in result_unknown

    # Plain non-flag argument (ref, path) passes through (branch -a has no commits but exits 0)
    tool_call_ref = {
        "id": "tc-4",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "branch", "args": ["-a"]}),
        },
    }
    result_ref = _execute_read_only_tool_call(tool_call_ref, str(tmp_path))
    assert _tool_succeeded(result_ref)

    # exec-related always-blocked flag is rejected
    tool_call_exec = {
        "id": "tc-5",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "status", "args": ["--exec=evil"]}),
        },
    }
    result_exec = _execute_read_only_tool_call(tool_call_exec, str(tmp_path))
    assert not _tool_succeeded(result_exec)
    assert "not permitted" in result_exec

    # Mutating remote sub-subcommand is rejected even though it is not a flag.
    # Previously this would slip through the flag-only check.
    tool_call_remote_add = {
        "id": "tc-6",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "remote", "args": ["add", "origin", "https://evil.example"]}),
        },
    }
    result_remote_add = _execute_read_only_tool_call(tool_call_remote_add, str(tmp_path))
    assert not _tool_succeeded(result_remote_add)
    assert "not a permitted sub-subcommand" in result_remote_add

    # read-only remote sub-subcommand is permitted
    tool_call_remote_show = {
        "id": "tc-7",
        "type": "function",
        "function": {
            "name": "git_inspect",
            "arguments": json.dumps({"subcommand": "remote", "args": ["-v"]}),
        },
    }
    result_remote_show = _execute_read_only_tool_call(tool_call_remote_show, str(tmp_path))
    # No remotes configured so output may be empty, but it must not be an error.
    assert _tool_succeeded(result_remote_show)


def test_client_uses_http_without_tls_config(monkeypatch):
    from core.client import _daemon_scheme
    monkeypatch.delenv("API_TLS_CERT", raising=False)
    monkeypatch.delenv("API_TLS_KEY", raising=False)
    assert _daemon_scheme() == "http"


def test_client_uses_https_with_tls_config(monkeypatch):
    from core.client import _daemon_scheme
    monkeypatch.setenv("API_TLS_CERT", "/tmp/cert.pem")
    monkeypatch.setenv("API_TLS_KEY", "/tmp/key.pem")
    assert _daemon_scheme() == "https"


def test_client_uses_http_when_only_one_tls_var_set(monkeypatch):
    from core.client import _daemon_scheme
    monkeypatch.setenv("API_TLS_CERT", "/tmp/cert.pem")
    monkeypatch.delenv("API_TLS_KEY", raising=False)
    assert _daemon_scheme() == "http"


def test_server_startup_with_invalid_tls_cert_raises(monkeypatch, tmp_path):
    from api.api_server import _build_ssl_context
    monkeypatch.setenv("API_TLS_CERT", str(tmp_path / "nonexistent.pem"))
    monkeypatch.setenv("API_TLS_KEY", str(tmp_path / "nonexistent.key"))
    with pytest.raises(RuntimeError, match="Failed to load TLS certificate"):
        _build_ssl_context()


def test_server_startup_with_only_cert_raises(monkeypatch, tmp_path):
    from api.api_server import _build_ssl_context
    monkeypatch.setenv("API_TLS_CERT", str(tmp_path / "cert.pem"))
    monkeypatch.delenv("API_TLS_KEY", raising=False)
    # Providing only one of cert/key is an explicit misconfiguration and must error.
    with pytest.raises(RuntimeError, match="Both API_TLS_CERT and API_TLS_KEY must be set"):
        _build_ssl_context()

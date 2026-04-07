import json
import os
import sys
import urllib.error
import urllib.request

from core.env_config import load_environment

DEFAULT_SESSION_ID = "default"


def _request_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("API_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def _resolve_daemon_host() -> str:
    host = os.environ.get("API_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        return "127.0.0.1"
    return host

def _daemon_scheme() -> str:
    """Return 'https' when the daemon is configured with TLS, 'http' otherwise."""
    cert = os.environ.get("API_TLS_CERT", "").strip()
    key = os.environ.get("API_TLS_KEY", "").strip()
    return "https" if (cert and key) else "http"

def _error_message_from_http_error(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:
        return str(error)
    return payload.get("error") or str(error)

def _send_request(endpoint: str, payload_dict: dict, timeout: int = 30) -> str:
    import ssl
    host = _resolve_daemon_host()
    port = os.environ.get("API_PORT", "8000")
    scheme = _daemon_scheme()
    url = f"{scheme}://{host}:{port}/{endpoint}"

    payload = json.dumps(payload_dict).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers=_request_headers(),
    )

    try:
        # When using https with a self-signed cert, allow the caller to opt out
        # of hostname verification via API_TLS_VERIFY=0.  Production deployments
        # should use a properly signed certificate and leave verification on.
        tls_verify = os.environ.get("API_TLS_VERIFY", "1").strip() not in ("0", "false", "no")
        ssl_ctx = ssl.create_default_context() if scheme == "https" else None
        if ssl_ctx and not tls_verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_ctx) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(_error_message_from_http_error(error)) from error
    except urllib.error.URLError as error:
        raise ConnectionError(f"Error connecting to jala-daemon at {host}:{port}: {error}") from error

    return data.get("response", "")

def send_message(message: str, cwd: str | None = None, session_id: str = DEFAULT_SESSION_ID, timeout: int = 30) -> str:
    return _send_request("chat", {
        "message": message,
        "cwd": cwd or os.getcwd(),
        "session_id": session_id,
    }, timeout)

def send_approval(action: str, approval_id: str, timeout: int = 30) -> str:
    return _send_request(action, {
        "approval_id": approval_id,
    }, timeout)


def _format_event_preview(event_data, max_len: int = 160) -> str:
    if isinstance(event_data, str):
        preview = event_data.strip()
    else:
        preview = json.dumps(event_data, ensure_ascii=True)
    preview = preview.replace("\n", "\\n")
    if len(preview) > max_len:
        return preview[: max_len - 3] + "..."
    return preview

def get_history(session_id=None, event_type=None, start_time=None, end_time=None, timeout: int = 30) -> str:
    import ssl
    import urllib.parse
    host = _resolve_daemon_host()
    port = os.environ.get("API_PORT", "8000")
    scheme = _daemon_scheme()

    query_params = {}
    if session_id: query_params["session_id"] = session_id
    if event_type: query_params["event_type"] = event_type
    if start_time: query_params["start_time"] = start_time
    if end_time: query_params["end_time"] = end_time

    query_string = urllib.parse.urlencode(query_params)
    url = f"{scheme}://{host}:{port}/history" + (f"?{query_string}" if query_string else "")

    request = urllib.request.Request(url, headers=_request_headers())

    try:
        tls_verify = os.environ.get("API_TLS_VERIFY", "1").strip() not in ("0", "false", "no")
        ssl_ctx = ssl.create_default_context() if scheme == "https" else None
        if ssl_ctx and not tls_verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_ctx) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(_error_message_from_http_error(error)) from error
    except urllib.error.URLError as error:
        raise ConnectionError(f"Error connecting to jala-daemon at {host}:{port}: {error}") from error

    history = data.get("history", {})
    output = []
    output.append("=== Recent Requests ===")
    for req in history.get("requests", []):
        output.append(f"[{req['timestamp']}] Session: {req['session_id']} | Message: {req['message']}")
    
    output.append("\n=== Recent Approvals ===")
    for app in history.get("approvals", []):
        err = f" | Error: {app['error_text']}" if app['error_text'] else ""
        output.append(f"[{app['timestamp']}] ID: {app['approval_id']} | Status: {app['status']} | Session: {app['session_id']}{err}")
    
    output.append("\n=== Recent Commands ===")
    for cmd in history.get("commands", []):
        output.append(f"[{cmd['timestamp']}] Approval ID: {cmd['approval_id']} | Command: {cmd['command']} | Exit Code: {cmd['exit_code']} | Duration: {cmd['duration']:.2f}s")
        if cmd['output']:
            output.append(f"  Output: {cmd['output'].strip()}")

    events = history.get("events", [])
    if events or session_id or event_type or start_time or end_time:
        output.append("\n=== Matching Events ===")
        if not events:
            output.append("(no matching events)")
        for event in events:
            output.append(
                f"[{event['timestamp']}] Event ID: {event['id']} | Session: {event['session_id']} | "
                f"Type: {event['event_type']} | Data: {_format_event_preview(event.get('event_data', ''))}"
            )

    return "\n".join(output)


def main(argv: list[str] | None = None) -> int:
    load_environment()
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        print("Usage: jala [chat] [-s|--session <session_id>] <message>")
        print("Usage: jala approve <approval_id>")
        print("Usage: jala deny <approval_id>")
        print("Usage: jala history [--session_id <id>] [--event_type <type>] [--start_time <time>] [--end_time <time>]")
        return 0

    session_id = DEFAULT_SESSION_ID

    if args[0] == "--interactive":
        print("Error: unknown option: --interactive", file=sys.stderr)
        return 1

    command = args[0]

    if command == "history":
        import argparse
        parser = argparse.ArgumentParser(prog="jala history", add_help=True)
        parser.add_argument("--session_id")
        parser.add_argument("--event_type")
        parser.add_argument("--start_time")
        parser.add_argument("--end_time")
        try:
            h_args, remaining = parser.parse_known_args(args[1:])
        except SystemExit:
            return 1
        if remaining:
            print(f"Error: unrecognized arguments for 'jala history': {' '.join(remaining)}", file=sys.stderr)
            parser.print_usage(sys.stderr)
            return 1
        try:
            print(get_history(h_args.session_id, h_args.event_type, h_args.start_time, h_args.end_time))
        except ConnectionError as error:
            print(str(error), file=sys.stderr)
            return 1
        except RuntimeError as error:
            print(f"Error from jala-daemon: {error}", file=sys.stderr)
            return 1
        return 0

    if command in ("approve", "deny") and len(args) == 2 and not args[1].startswith("-"):
        action = args[0]
        approval_id = args[1]
        try:
            print(send_approval(action, approval_id))
            return 0
        except ConnectionError as error:
            print(str(error), file=sys.stderr)
            return 1
        except RuntimeError as error:
            print(f"Error from jala-daemon: {error}", file=sys.stderr)
            return 1

    if command in ("approve", "deny"):
        print(f"Usage: jala {command} <approval_id>", file=sys.stderr)
        return 1

    MESSAGE_PREFIXES = {"chat", "message", "say", "--", "--message", "-m"}
    if command in MESSAGE_PREFIXES:
        args = args[1:]
        if not args:
            print("Error: Message cannot be empty.", file=sys.stderr)
            return 1

    if args and args[0] in ("-s", "--session"):
        if len(args) < 2:
            print("Error: --session requires a session id.", file=sys.stderr)
            return 1
        session_id = args[1]
        args = args[2:]

    if args and args[0] == "--":
        args = args[1:]

    if not args:
        print("Error: Message cannot be empty.", file=sys.stderr)
        return 1

    if args[0].startswith("-"):
        print(f"Error: Unknown option: {args[0]}", file=sys.stderr)
        return 1

    message = " ".join(args).strip()
    if not message:
        print("Error: Message cannot be empty.", file=sys.stderr)
        return 1

    try:
        print(send_message(message, cwd=os.getcwd(), session_id=session_id))
    except ConnectionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"Error from jala-daemon: {error}", file=sys.stderr)
        return 1

    return 0

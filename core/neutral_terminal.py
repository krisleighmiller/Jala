import logging
import os
import subprocess
import threading

from core.env_config import load_environment

load_environment()

logger = logging.getLogger("jala.terminal")
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


class _LimitedBuffer:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, max_bytes)
        self._chunks: list[bytes] = []
        self._stored = 0
        self._discarded = 0
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            remaining = self.max_bytes - self._stored
            if remaining > 0:
                kept = chunk[:remaining]
                self._chunks.append(kept)
                self._stored += len(kept)
                self._discarded += len(chunk) - len(kept)
            else:
                self._discarded += len(chunk)

    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", errors="replace")

    @property
    def discarded_bytes(self) -> int:
        with self._lock:
            return self._discarded


class NeutralTerminal:
    _openai_client = None
    _openai_client_key = None

    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        self.default_temperature = 0.0
        self.max_output_bytes = int(
            os.environ.get("JALA_MAX_OUTPUT_BYTES", str(DEFAULT_MAX_OUTPUT_BYTES))
        )

    def _collect_limited_output(self, process: subprocess.Popen):
        stdout_buf = _LimitedBuffer(self.max_output_bytes)
        stderr_buf = _LimitedBuffer(self.max_output_bytes)

        def _drain(stream, buffer: _LimitedBuffer):
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    buffer.add(chunk)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        out_thread = threading.Thread(target=_drain, args=(process.stdout, stdout_buf), daemon=True)
        err_thread = threading.Thread(target=_drain, args=(process.stderr, stderr_buf), daemon=True)
        out_thread.start()
        err_thread.start()
        return stdout_buf, stderr_buf, out_thread, err_thread

    def _run_process_limited(self, popen_kwargs: dict, timeout):
        process = subprocess.Popen(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            **popen_kwargs,
        )
        stdout_buf, stderr_buf, out_thread, err_thread = self._collect_limited_output(process)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return f"Command timed out after {timeout} seconds.", 124
        finally:
            out_thread.join(timeout=1.0)
            err_thread.join(timeout=1.0)

        output = f"{stdout_buf.text()}\n{stderr_buf.text()}".strip()
        discarded = stdout_buf.discarded_bytes + stderr_buf.discarded_bytes
        if discarded > 0:
            note = f"[output truncated: discarded {discarded} bytes]"
            output = f"{output}\n{note}".strip() if output else note
        return output, process.returncode

    def execute_local(self, command, cwd=None, timeout=None):
        try:
            return self._run_process_limited(
                {
                    "args": command,
                    "shell": True,
                    "cwd": cwd,
                },
                timeout,
            )
        except Exception as e:
            logger.exception("Local shell command failed")
            return str(e), 1

    def execute_local_args(self, args, cwd=None, timeout=None):
        try:
            return self._run_process_limited(
                {
                    "args": args,
                    "shell": False,
                    "cwd": cwd,
                },
                timeout,
            )
        except FileNotFoundError as e:
            return str(e), 127
        except Exception as e:
            logger.exception("Local argv command failed")
            return str(e), 1

    def _get_openai_client(self):
        if (
            NeutralTerminal._openai_client is None
            or NeutralTerminal._openai_client_key != self.api_key
        ):
            from openai import OpenAI

            NeutralTerminal._openai_client = OpenAI(api_key=self.api_key)
            NeutralTerminal._openai_client_key = self.api_key
        return NeutralTerminal._openai_client

    def connect_to_chatgpt_messages(
        self,
        messages,
        model=None,
        max_tokens=None,
        temperature=None,
        format="json_object",
        timeout=None,
        tools=None,
    ):
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        if not model:
            model = self.model
        if temperature is None:
            temperature = self.default_temperature

        client = self._get_openai_client()
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message

"""Minimal OpenAI-compatible fake LLM server for streaming and tool-progress checks."""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "fake-model"
CALL_ID = "call_fake_1"
FIRST_TEXT = "正在检查文件..."
FINAL_TEXT = "README.md 已读取，内容完整，验收通过。"
TOOL_ARGS = {"path": "README.md"}


def _split(text: str, parts: int) -> list[str]:
    size = len(text)
    bounds = [round(i * size / parts) for i in range(parts + 1)]
    return [piece for a, b in zip(bounds, bounds[1:]) if (piece := text[a:b])]


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": CALL_ID,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _usage() -> dict[str, int]:
    return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class FakeLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config: argparse.Namespace

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        round_no = 2 if any(m.get("role") == "tool" for m in messages if isinstance(m, dict)) else 1
        stream = bool(body.get("stream"))
        print(
            json.dumps({"t": round(time.time(), 3), "round": round_no, "stream": stream}),
            file=sys.stderr,
            flush=True,
        )
        if stream:
            self._stream(round_no)
        else:
            delay = self.config.chunk_delay if round_no == 1 else self.config.final_delay
            time.sleep(delay * (7 if round_no == 1 else 8))
            self._json(self._complete(round_no))

    def _tool_call(self) -> dict[str, Any]:
        return {
            "id": CALL_ID,
            "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps(TOOL_ARGS)},
        }

    def _complete(self, round_no: int) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": FIRST_TEXT if round_no == 1 else self.config.final_text,
        }
        finish = "stop"
        if round_no == 1:
            message["tool_calls"] = [self._tool_call()]
            finish = "tool_calls"
        return {
            "id": CALL_ID,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": _usage(),
        }

    def _stream(self, round_no: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        delay = self.config.chunk_delay if round_no == 1 else self.config.final_delay
        text = FIRST_TEXT if round_no == 1 else self.config.final_text
        parts = 5 if round_no == 1 else 8
        for piece in _split(text, parts):
            self._sse(_chunk({"role": "assistant", "content": piece}))
            time.sleep(delay)
        if round_no == 1:
            arguments = json.dumps(TOOL_ARGS)
            mid = len(arguments) // 2
            head = {"index": 0, "id": CALL_ID, "type": "function", "function": {"name": "read_file", "arguments": arguments[:mid]}}
            tail = {"index": 0, "function": {"arguments": arguments[mid:]}}
            for fragment in (head, tail):
                self._sse(_chunk({"role": "assistant", "tool_calls": [fragment]}))
                time.sleep(delay)
            self._sse(_chunk({}, "tool_calls"))
        else:
            self._sse(_chunk({}, "stop"))
        self._sse({"id": CALL_ID, "object": "chat.completion.chunk", "model": MODEL, "choices": [], "usage": _usage()})
        self._sse("[DONE]")

    def _sse(self, payload: dict[str, Any] | str) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible fake LLM server")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--chunk-delay", type=float, default=0.3)
    parser.add_argument("--final-delay", type=float, default=0.2)
    parser.add_argument("--final-text", default=FINAL_TEXT)
    args = parser.parse_args()
    FakeLLMHandler.config = args
    server = ThreadingHTTPServer((args.host, args.port), FakeLLMHandler)
    print(f"fake_llm listening on http://{args.host}:{server.server_address[1]}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

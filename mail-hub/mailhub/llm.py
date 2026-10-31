"""LLM backends. Each one takes a prompt string and returns the model's text.

  claude_cli  `claude -p` on your Claude subscription (CLAUDE_CODE_OAUTH_TOKEN
              from `claude setup-token`, or the machine's existing login)
  anthropic   Anthropic Messages API, pay per token (ANTHROPIC_API_KEY)
  ollama      local model via Ollama's HTTP API
"""
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request

# Email bodies are untrusted input. The CLI backend runs with every tool
# disabled, in an empty directory, so a hostile email can at worst produce a
# wrong classification.
CLAUDE_DISALLOWED = ("Bash,Edit,Write,MultiEdit,Read,Glob,Grep,LS,WebFetch,WebSearch,"
                     "NotebookEdit,NotebookRead,Task,TodoWrite")


class LLMError(Exception):
    pass


def complete(cfg, prompt):
    backend = cfg.llm.get("backend", "claude_cli")
    opts = cfg.llm.get(backend, {})
    if backend == "claude_cli":
        return _claude_cli(opts, prompt)
    if backend == "anthropic":
        return _anthropic(opts, prompt)
    if backend == "ollama":
        return _ollama(opts, prompt)
    raise LLMError(f"unknown llm backend {backend!r}")


def _claude_cli(opts, prompt):
    cmd = [opts.get("command", "claude"), "-p", "--output-format", "json",
           "--model", opts.get("model", "haiku"),
           "--max-turns", "1", "--disallowedTools", CLAUDE_DISALLOWED]
    with tempfile.TemporaryDirectory(prefix="mailhub-") as cwd:
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                               timeout=int(opts.get("timeout", 300)))
        except FileNotFoundError:
            raise LLMError(f"找不到 {cmd[0]}，在 config 的 [llm.claude_cli] command 里写完整路径") from None
        except subprocess.TimeoutExpired:
            raise LLMError("claude -p timed out") from None
    if r.returncode != 0:
        raise LLMError(f"claude -p exit {r.returncode}: {(r.stderr or r.stdout)[-500:]}")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout
    if out.get("is_error"):
        raise LLMError(f"claude -p error: {str(out.get('result'))[:500]}")
    return out.get("result", "")


def _http_json(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from None


def _anthropic(opts, prompt):
    key = os.environ.get(opts.get("api_key_env", "ANTHROPIC_API_KEY"))
    if not key:
        raise LLMError("ANTHROPIC_API_KEY 没有设置")
    out = _http_json(
        "https://api.anthropic.com/v1/messages",
        {"model": opts.get("model", "claude-haiku-5-5"), "max_tokens": 8000,
         "messages": [{"role": "user", "content": prompt}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        int(opts.get("timeout", 180)),
    )
    return "".join(b.get("text", "") for b in out.get("content", []))


def _ollama(opts, prompt):
    out = _http_json(
        opts.get("url", "http://127.0.0.1:11434").rstrip("/") + "/api/chat",
        {"model": opts.get("model", "qwen3:8b"), "stream": False, "format": "json",
         "options": {"temperature": 0},
         "messages": [{"role": "user", "content": prompt}]},
        {}, int(opts.get("timeout", 600)),
    )
    return out.get("message", {}).get("content", "")

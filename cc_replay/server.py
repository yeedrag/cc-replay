"""cc-replay: browse Claude Code transcripts, fork and continue conversations.

Displays a JSONL transcript in a web UI. When you fork, it:
1. Truncates the JSONL and writes a new CC session
2. Spawns Claude Code with the forked session
3. Streams output back to the browser via SSE
4. Intercepts tool permission requests via a hook and shows them in the browser

Usage:
    cc-replay transcript.jsonl              # load a specific transcript
    cc-replay transcript.jsonl --cwd /path  # set working dir for Claude Code
    cc-replay                               # start empty, upload via browser
"""

import argparse
import asyncio
import json
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI()

# --- Global state ---
transcript_events: list[dict] = []
raw_lines: list[str] = []
transcript_path: Path | None = None
transcript_session_id: str = ""
cc_projects_dir: Path = Path()
cc_work_dir: Path = Path()
anthropic_api_key: str = ""

running_processes: dict[str, asyncio.subprocess.Process] = {}
pending_permissions: dict[str, asyncio.Future] = {}
permission_history: list[dict] = []


# --- Transcript Parsing ---

def parse_transcript(path_or_text: Path | str) -> tuple[list[dict], list[str], str]:
    if isinstance(path_or_text, Path):
        with open(path_or_text, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = path_or_text.splitlines(keepends=True)

    raw_events = []
    original_session_id = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
            event["_line_idx"] = i
            event["_line_num"] = i + 1
            if not original_session_id and event.get("sessionId"):
                original_session_id = event["sessionId"]
            raw_events.append(event)
        except json.JSONDecodeError:
            continue

    conv_events = [
        e for e in raw_events
        if e.get("type") in ("user", "assistant")
        and not e.get("isSidechain", False)
    ]

    display: list[dict] = []
    i = 0
    while i < len(conv_events):
        event = conv_events[i]

        if event["type"] == "user":
            display.append({
                "index": len(display),
                "type": "user",
                "content": event.get("message", {}).get("content", ""),
                "uuid": event.get("uuid", ""),
                "timestamp": event.get("timestamp", ""),
                "is_meta": event.get("isMeta", False),
                "line_num": event["_line_num"],
                "line_idx": event["_line_idx"],
            })
            i += 1

        elif event["type"] == "assistant":
            msg = event.get("message", {})
            msg_id = msg.get("id", "")
            content_blocks: list[dict] = []
            model = msg.get("model")
            usage = msg.get("usage")
            stop_reason = msg.get("stop_reason")
            last_line_idx = event["_line_idx"]

            raw_content = msg.get("content", [])
            if isinstance(raw_content, list):
                content_blocks.extend(raw_content)
            elif isinstance(raw_content, str):
                content_blocks.append({"type": "text", "text": raw_content})

            j = i + 1
            while j < len(conv_events):
                next_ev = conv_events[j]
                if next_ev["type"] != "assistant":
                    break
                next_msg = next_ev.get("message", {})
                if next_msg.get("id") != msg_id:
                    break
                next_content = next_msg.get("content", [])
                if isinstance(next_content, list):
                    content_blocks.extend(next_content)
                if next_msg.get("stop_reason"):
                    stop_reason = next_msg["stop_reason"]
                if next_msg.get("usage"):
                    usage = next_msg["usage"]
                last_line_idx = next_ev["_line_idx"]
                j += 1

            display.append({
                "index": len(display),
                "type": "assistant",
                "content": content_blocks,
                "uuid": event.get("uuid", ""),
                "timestamp": event.get("timestamp", ""),
                "model": model,
                "usage": usage,
                "stop_reason": stop_reason,
                "msg_id": msg_id,
                "line_num": event["_line_num"],
                "line_idx": event["_line_idx"],
                "last_line_idx": last_line_idx,
            })
            i = j
        else:
            i += 1

    return display, lines, original_session_id


def get_truncation_line_idx(display_events: list[dict], fork_index: int) -> int:
    if fork_index + 1 < len(display_events):
        return display_events[fork_index + 1]["line_idx"]
    return len(raw_lines)


def event_ends_with_tool_use(event: dict) -> bool:
    if event["type"] != "assistant":
        return False
    content = event.get("content", [])
    if not isinstance(content, list):
        return False
    return any(b.get("type") == "tool_use" for b in content if isinstance(b, dict))


def extract_tool_result_content(display_events: list[dict], after_index: int) -> list[dict]:
    all_blocks: list[dict] = []
    i = after_index + 1
    while i < len(display_events):
        ev = display_events[i]
        if ev["type"] != "user":
            break
        content = ev.get("content")
        if isinstance(content, list):
            has_tr = any(b.get("type") == "tool_result" for b in content if isinstance(b, dict))
            if has_tr:
                all_blocks.extend(content)
                i += 1
                continue
        if ev.get("is_meta", False):
            i += 1
            continue
        break
    return all_blocks


def find_cc_projects_dir(work_dir: Path | None = None) -> Path:
    claude_dir = Path.home() / ".claude" / "projects"
    cwd = (work_dir or Path.cwd()).resolve()
    encoded = re.sub(r'[^a-zA-Z0-9]', '-', str(cwd))
    return claude_dir / encoded


# --- Endpoints ---

@app.get("/")
async def serve_index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "loaded": len(transcript_events) > 0,
        "num_events": len(transcript_events),
        "num_raw_lines": len(raw_lines),
        "transcript_path": str(transcript_path) if transcript_path else None,
    })


@app.get("/api/transcript")
async def get_transcript():
    return JSONResponse({"events": transcript_events})


@app.get("/api/config")
async def get_config():
    return JSONResponse({
        "num_events": len(transcript_events),
        "num_raw_lines": len(raw_lines),
        "cc_projects_dir": str(cc_projects_dir),
        "hook_script": str(Path(__file__).parent / "hook.py"),
    })


@app.post("/api/upload")
async def upload_transcript(file: UploadFile = File(...)):
    global transcript_events, raw_lines, transcript_path, transcript_session_id
    content = await file.read()
    text = content.decode("utf-8")
    transcript_events, raw_lines, transcript_session_id = parse_transcript(text)
    transcript_path = None
    return JSONResponse({
        "num_events": len(transcript_events),
        "num_raw_lines": len(raw_lines),
        "session_id": transcript_session_id,
    })


@app.post("/api/fork-and-run")
async def fork_and_run(request: Request):
    body = await request.json()
    fork_index = body["fork_index"]
    prompt = body.get("prompt", "")
    effort = body.get("effort", "high")

    if fork_index < 0 or fork_index >= len(transcript_events):
        return JSONResponse({"error": "Invalid fork index"}, status_code=400)

    pending_permissions.clear()
    permission_history.clear()

    trunc_line_idx = get_truncation_line_idx(transcript_events, fork_index)
    truncated_lines = raw_lines[:trunc_line_idx]

    new_session_id = str(uuid.uuid4())
    cc_projects_dir.mkdir(parents=True, exist_ok=True)
    session_file = cc_projects_dir / f"{new_session_id}.jsonl"
    session_file.write_text("".join(truncated_lines), encoding="utf-8")

    hook_script = Path(__file__).parent / "hook.py"
    python_bin = sys.executable
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return JSONResponse({"error": "Claude Code CLI not found in PATH"}, status_code=500)

    settings_data = {
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": f'{python_bin} "{hook_script}" http://127.0.0.1:{server_port}',
                }],
            }],
        },
    }
    settings_file = cc_projects_dir / f"{new_session_id}-settings.json"
    settings_file.write_text(json.dumps(settings_data), encoding="utf-8")

    cmd = [
        claude_bin,
        "--resume", new_session_id,
        "--fork-session",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--effort", effort,
        "--settings", str(settings_file),
    ]

    fork_event = transcript_events[fork_index]
    mode = "unknown"
    tool_result_blocks: list[dict] = []
    stdin_data: bytes | None = None

    if event_ends_with_tool_use(fork_event) and not prompt:
        tool_result_blocks = extract_tool_result_content(transcript_events, fork_index)
        if tool_result_blocks:
            stdin_msg = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": tool_result_blocks},
            }) + "\n"
            stdin_data = stdin_msg.encode("utf-8")
            cmd.extend(["--input-format", "stream-json"])
            mode = "tool_result"
        else:
            return JSONResponse(
                {"error": "Fork event has tool_use but no tool_result found. Please type a prompt."},
                status_code=400,
            )
    elif prompt:
        cmd.append(prompt)
        mode = "text_prompt"
    else:
        return JSONResponse(
            {"error": "This fork point requires a prompt. Only assistant events with tool_use can continue empty."},
            status_code=400,
        )

    import os
    fork_env = {**os.environ, "ANTHROPIC_API_KEY": anthropic_api_key} if anthropic_api_key else None

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        cwd=str(cc_work_dir),
        env=fork_env,
    )

    if stdin_data and process.stdin:
        process.stdin.write(stdin_data)
        await process.stdin.drain()
        process.stdin.close()

    process_id = new_session_id
    running_processes[process_id] = process

    resp: dict[str, Any] = {
        "process_id": process_id,
        "session_id": new_session_id,
        "fork_at_event": fork_index,
        "fork_at_line": transcript_events[fork_index]["line_num"],
        "truncated_lines": len(truncated_lines),
        "total_lines": len(raw_lines),
        "mode": mode,
    }
    if tool_result_blocks:
        resp["tool_result_sent"] = tool_result_blocks
    return JSONResponse(resp)


@app.get("/api/stream/{process_id}")
async def stream_output(process_id: str):
    process = running_processes.get(process_id)
    if not process:
        return JSONResponse({"error": "Process not found"}, status_code=404)

    from fastapi.responses import StreamingResponse

    async def generate():
        stderr_lines: list[str] = []

        async def read_stderr():
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)

        stderr_task = asyncio.create_task(read_stderr())

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    yield f"data: {text}\n\n"

            await process.wait()
            stderr_task.cancel()

            if stderr_lines:
                yield f"data: {json.dumps({'type': 'stderr', 'lines': stderr_lines[-20:]})}\n\n"
            yield f"data: {json.dumps({'type': 'process_exit', 'return_code': process.returncode})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            running_processes.pop(process_id, None)
            _do_cleanup(process_id)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/stop/{process_id}")
async def stop_process(process_id: str):
    process = running_processes.pop(process_id, None)
    if process:
        process.kill()
        _do_cleanup(process_id)
        return JSONResponse({"stopped": True})
    return JSONResponse({"stopped": False})


# --- Permission Hook Endpoints ---

@app.post("/hook/permission")
async def handle_permission_request(request: Request):
    body = await request.json()
    req_id = body.get("tool_use_id", str(uuid.uuid4()))

    permission_entry = {
        "id": req_id,
        "tool_name": body.get("tool_name", "?"),
        "tool_input": body.get("tool_input", {}),
        "timestamp": body.get("timestamp"),
        "decided": False,
        "decision": None,
    }
    permission_history.append(permission_entry)

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    pending_permissions[req_id] = future

    try:
        decision = await asyncio.wait_for(future, timeout=600)
    except asyncio.TimeoutError:
        decision = {"behavior": "deny"}
    finally:
        pending_permissions.pop(req_id, None)

    permission_entry["decided"] = True
    permission_entry["decision"] = decision
    return JSONResponse({"decision": decision})


@app.get("/api/permissions")
async def get_permissions():
    pending = [p for p in permission_history if not p["decided"]]
    recent = [p for p in permission_history if p["decided"]][-10:]
    return JSONResponse({"pending": pending, "recent": recent})


@app.post("/api/permissions/{req_id}/decide")
async def decide_permission(req_id: str, request: Request):
    body = await request.json()
    behavior = body.get("behavior", "deny")

    decision = {"behavior": behavior}
    if "updatedInput" in body:
        decision["updatedInput"] = body["updatedInput"]

    future = pending_permissions.get(req_id)
    if future and not future.done():
        future.set_result(decision)
        return JSONResponse({"ok": True})

    return JSONResponse({"error": "No pending request with that ID"}, status_code=404)


def _do_cleanup(session_id: str) -> list[str]:
    deleted = []
    for suffix in [".jsonl", "-settings.json"]:
        f = cc_projects_dir / f"{session_id}{suffix}"
        if f.exists():
            f.unlink()
            deleted.append(str(f))
    session_dir = cc_projects_dir / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
        deleted.append(str(session_dir))
    return deleted


@app.post("/api/cleanup/{session_id}")
async def cleanup_session(session_id: str):
    return JSONResponse({"deleted": _do_cleanup(session_id)})


# --- Startup ---

server_port: int = 8765


def main():
    global transcript_events, raw_lines, transcript_path, transcript_session_id
    global cc_projects_dir, cc_work_dir, server_port, anthropic_api_key

    parser = argparse.ArgumentParser(description="View, replay, and fork Claude Code transcripts")
    parser.add_argument("transcript", nargs="?", help="Path to a JSONL transcript file (optional — can upload via browser)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cwd", help="Working directory for Claude Code (default: cwd or transcript parent)")
    parser.add_argument("--api-key-file", help="Path to file containing ANTHROPIC_API_KEY (uses API key auth instead of OAuth, avoids email leak)")
    args = parser.parse_args()

    server_port = args.port

    if args.transcript:
        transcript_path = Path(args.transcript).resolve()
        assert transcript_path.exists(), f"Transcript not found: {transcript_path}"
        print(f"Loading transcript: {transcript_path}")
        transcript_events, raw_lines, transcript_session_id = parse_transcript(transcript_path)
        print(f"  {len(transcript_events)} display events, {len(raw_lines)} raw lines")
    else:
        print("No transcript specified — upload one via the browser.")

    cc_work_dir = Path(args.cwd).resolve() if args.cwd else (
        transcript_path.parent if transcript_path else Path.cwd()
    )
    cc_projects_dir = find_cc_projects_dir(cc_work_dir)
    if args.api_key_file:
        key_text = Path(args.api_key_file).read_text().strip()
        anthropic_api_key = key_text.split("=", 1)[-1].strip() if "=" in key_text else key_text
        print(f"  API key: loaded from {args.api_key_file} (no OAuth email)")
    print(f"  CC work dir: {cc_work_dir}")
    print(f"  CC projects dir: {cc_projects_dir}")

    print(f"\nStarting server at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

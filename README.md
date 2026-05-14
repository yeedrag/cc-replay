# cc-replay

View, replay, and fork Claude Code transcripts in the browser.

Load any Claude Code `.jsonl` transcript, browse the conversation with rich tool call rendering, and **fork** at any point to continue the conversation with a live Claude Code session — with tool permission approval handled in the browser.

## Features

- **Transcript viewer** — renders user messages, assistant responses, thinking blocks, and tool calls with syntax-highlighted diffs, file paths, and command formatting
- **Fork & continue** — click "Fork" on any message to truncate the transcript there and spawn a new Claude Code session that continues from that point
- **Browser-based permissions** — when Claude Code requests tool permissions during a forked session, approve or deny them directly in the browser
- **Upload or CLI** — pass a transcript file on the command line, or upload one through the browser

## Install

```bash
# With uv (recommended)
uv tool install .

# Or with pip
pip install .
```

## Usage

```bash
# Load a specific transcript
cc-replay path/to/transcript.jsonl

# Start empty, upload via browser
cc-replay

# Custom port and working directory
cc-replay transcript.jsonl --port 9000 --cwd /path/to/project
```

Then open `http://127.0.0.1:8765` in your browser.

### Finding your transcripts

Claude Code stores transcripts in `~/.claude/projects/<encoded-path>/`. Each session is a `.jsonl` file named by its session UUID.

## How forking works

1. Click "Fork" on any message in the transcript
2. Optionally type a new prompt (required unless the last assistant message ended with a tool call)
3. Click "Run" — cc-replay truncates the transcript at that point and spawns `claude --resume <session> --fork-session`
4. Claude Code's streaming output appears in the browser in real time
5. Tool permission requests are intercepted via a [PreToolUse hook](https://docs.anthropic.com/en/docs/claude-code/hooks) and shown as approve/deny cards in the browser

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and on PATH (for the fork feature)

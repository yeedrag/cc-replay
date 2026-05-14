"""PreToolUse hook script for Claude Code.

Claude Code runs this before every tool execution. It:
1. Reads the tool call details from stdin
2. POSTs them to the cc-replay web server
3. The web server blocks until the user approves/denies in the browser
4. Returns the decision as JSON on stdout
"""

import json
import sys
import urllib.request


SERVER_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def main():
    raw = sys.stdin.read()
    try:
        request_data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    payload = json.dumps(request_data).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER_URL}/hook/permission",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=660) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            decision = result.get("decision", {})
            behavior = decision.get("behavior", "allow")
    except Exception:
        behavior = "allow"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": behavior,
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()

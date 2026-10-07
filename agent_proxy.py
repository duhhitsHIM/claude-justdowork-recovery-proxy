import os
import re
import sys
import copy
import json
import uuid
from datetime import datetime
import requests
from flask import Flask, request, Response, stream_with_context

# Under the tray launcher stdout is a redirected file, so Python encodes it with
# the ANSI codepage (cp1252); a reply containing emoji then crashes log() with
# UnicodeEncodeError and the request dies with a 500. Force UTF-8.
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

app = Flask(__name__)

TARGET = os.environ.get("TARGET_URL", "https://api.justwoker.icu").rstrip("/")
KEY = re.sub(r"[^\x21-\x7e]", "", os.environ.get("UPSTREAM_API_KEY", ""))
PORT = int(os.environ.get("PORT", "8181"))
LOG = "debug_log.txt"
DUMP_DIR = "debug_dump"
# Request/response dumps contain full system prompts and file contents, so they are
# opt-in only: set DEBUG_DUMP=1 to write them.
DUMP = os.environ.get("DEBUG_DUMP", "").strip().lower() in ("1", "true", "yes", "on")
counter = {"n": 0}

ALLOWED_TOP = ["model", "max_tokens", "messages", "system", "tools", "tool_choice",
               "temperature", "top_p", "top_k", "stop_sequences"]

# Tools the relay passes through natively (confirmed via names_probe.py).
# All other tools (TodoWrite, WebSearch, Agent/Task, Skill, workflows, MCP...)
# have no native counterpart on the relay -- they run via the text protocol (<tool_call>).
NAME_MAP = {"Read": "read", "Write": "write", "Edit": "edit", "Bash": "bash",
            "Glob": "glob", "Grep": "grep", "WebFetch": "web_fetch"}
_extra = os.environ.get("NAME_MAP_EXTRA")  # e.g. '{"Grep":"grep"}' if the probe discovers a new name
if _extra:
    NAME_MAP.update(json.loads(_extra))
REV_MAP = {v: k for k, v in NAME_MAP.items()}

CALL_RE = re.compile(r'<tool_call\s+name="([^"]+)"\s*>(.*?)</tool_call>', re.DOTALL)
DESC_LIMIT = 2500

# Usage rewriting: the relay pads input usage (a 6-char message reports ~20k
# input tokens), and clients read usage as context occupancy -- inflated numbers
# make them refuse tool calls ("context full"). Reported input may exceed our
# own estimate by this band before we rewrite it; tokenizer variance fits inside.
USAGE_BAND = 1.25
USAGE_MARGIN = 4096


REDACTED = "[REDACTED]"
# Key-shaped tokens that must never reach stdout or debug_log.txt. Patterns with a
# capture group keep the label (e.g. "Bearer ") and replace only the secret itself.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(bearer\s+)[A-Za-z0-9._\-]{12,}", re.I),
    re.compile(r"((?:x-api-key|api[_-]?key|authorization)[\"'\s]*[:=][\"'\s]*)[A-Za-z0-9._\-]{12,}",
               re.I),
]


def redact(msg):
    s = msg if isinstance(msg, str) else str(msg)
    if KEY and len(KEY) >= 8:
        s = s.replace(KEY, REDACTED)
    for pat in SECRET_PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: m.group(1) + REDACTED, s)
        else:
            s = pat.sub(REDACTED, s)
    return s


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {redact(msg)}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def dump_json(name, obj):
    """Write a debug dump, but only when DEBUG_DUMP is enabled."""
    if not DUMP:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        with open(os.path.join(DUMP_DIR, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"dump failed ({name}): {e}")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def head(x, n=300):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s[:n].replace("\n", "\\n")


def strip_key(obj, key):
    if isinstance(obj, dict):
        return {k: strip_key(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [strip_key(v, key) for v in obj]
    return obj


def flatten_result(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_reference":
            parts.append(f"[tool: {b.get('tool_name', '')}]")
        else:
            parts.append(f"[{t}]")
    return "\n".join(parts)


def clean_inner(content):
    """Clean up blocks inside tool_result content."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("thinking", "redacted_thinking"):
            continue
        if t == "tool_reference":
            out.append({"type": "text", "text": f"[tool: {b.get('tool_name', '')}]"})
            continue
        out.append(b)
    return out if out else "(empty)"


def collect_emulated(messages):
    """Collect id -> name for emulated (text-protocol) tool_use blocks in history."""
    ids = {}
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") not in NAME_MAP):
                    ids[b.get("id")] = b.get("name")
    return ids


def convert_messages(messages, emulated_ids):
    out = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            out.append({"role": m.get("role"), "content": c})
            continue
        results, others, emu_results = [], [], []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in ("thinking", "redacted_thinking"):
                continue
            if t == "tool_reference":
                others.append({"type": "text", "text": f"[tool: {b.get('tool_name', '')}]"})
            elif t == "tool_use":
                if b.get("name") in NAME_MAP:
                    nb = dict(b)
                    nb["name"] = NAME_MAP[b["name"]]
                    others.append(nb)
                else:
                    call = json.dumps(b.get("input", {}), ensure_ascii=False)
                    others.append({"type": "text",
                                   "text": f'<tool_call name="{b.get("name")}">{call}</tool_call>'})
            elif t == "tool_result":
                tid = b.get("tool_use_id")
                if tid in emulated_ids:
                    err = ' error="true"' if b.get("is_error") else ""
                    emu_results.append({"type": "text", "text":
                        f'<tool_result name="{emulated_ids[tid]}" id="{tid}"{err}>\n'
                        f'{flatten_result(b.get("content"))}\n</tool_result>'})
                else:
                    nb = dict(b)
                    if isinstance(nb.get("content"), list):
                        nb["content"] = clean_inner(nb["content"])
                    results.append(nb)
            else:
                others.append(b)
        new = results + others + emu_results
        if not new:
            new = [{"type": "text", "text": "(empty)"}]
        out.append({"role": m.get("role"), "content": new})
    return out


def build_extra_prompt(tools):
    extra = [t for t in tools if "input_schema" in t and t.get("name") not in NAME_MAP]
    native = ", ".join(f"{v} (= {k})" for k, v in NAME_MAP.items())
    lines = [
        "## Tool names in this session",
        f"Native tools use lowercase names: {native}. Call them through the normal tool-calling "
        "mechanism, using the same parameters as the original tool of that name. "
        "Ignore any tool named read_tabular or system_todo_write; they are not available.",
    ]
    if extra:
        lines += [
            "",
            "## Extra tools (text protocol)",
            "The tools listed below are NOT in your native tool list. To call one, write a block "
            "exactly like this in your reply:",
            '<tool_call name="TOOL_NAME">{"param": "value"}</tool_call>',
            "- The body must be one valid JSON object matching the tool's input schema.",
            "- You may write normal text before a tool_call and may emit several tool_call blocks "
            "in one reply (for example to run several agents or searches in parallel).",
            "- After the last tool_call block STOP writing. Never guess or invent results.",
            "- The result arrives in the next user message as "
            '<tool_result name="TOOL_NAME" id="...">...</tool_result>.',
            "- Use these tools only through <tool_call> blocks, never through the native mechanism.",
            "",
        ]
        for t in extra:
            desc = (t.get("description") or "")[:DESC_LIMIT]
            schema = json.dumps(strip_key(t["input_schema"], "$schema"), separators=(",", ":"))
            lines.append(f"### {t.get('name')}\n{desc}\nInput schema: {schema}\n")
    return "\n".join(lines)


def sanitize(body, aggressive=False):
    out = {k: copy.deepcopy(body[k]) for k in ALLOWED_TOP if k in body}
    out = strip_key(out, "cache_control")
    emulated_ids = collect_emulated(out.get("messages", []))

    all_tools = [t for t in (out.get("tools") or []) if isinstance(t, dict)]
    extra_text = build_extra_prompt(all_tools) if all_tools else None

    # system
    sysv = out.get("system")
    if isinstance(sysv, list):
        texts = [b.get("text", "") for b in sysv if isinstance(b, dict)]
        sysv = "\n\n".join(texts) if aggressive else [{"type": "text", "text": t} for t in texts]
    if extra_text:
        if sysv is None:
            sysv = extra_text
        elif isinstance(sysv, str):
            sysv = sysv + "\n\n" + extra_text
        else:
            sysv = sysv + [{"type": "text", "text": extra_text}]
    if sysv is not None:
        out["system"] = sysv

    # native tools only
    native = []
    for t in all_tools:
        if "input_schema" not in t or t.get("name") not in NAME_MAP:
            continue
        schema = t["input_schema"]
        if aggressive:
            schema = strip_key(schema, "$schema")
        native.append({"name": NAME_MAP[t["name"]], "description": t.get("description", ""),
                       "input_schema": schema})
    if native:
        out["tools"] = native
        tc = out.get("tool_choice")
        if isinstance(tc, dict) and tc.get("name"):
            if tc["name"] in NAME_MAP:
                tc["name"] = NAME_MAP[tc["name"]]
            else:
                out.pop("tool_choice", None)
    else:
        out.pop("tools", None)
        out.pop("tool_choice", None)

    out["messages"] = convert_messages(out.get("messages", []), emulated_ids)
    out["stream"] = False
    return out


def parse_response(msg, emulated_names):
    """Parse relay response into blocks for Claude Code (native + text-protocol tool calls)."""
    out, has_tool = [], False
    for b in msg.get("content", []):
        t = b.get("type")
        if t == "tool_use":
            name = REV_MAP.get(b.get("name"))
            if not name:
                log(f"DROPPED unknown relay tool_use: {b.get('name')}")
                continue
            b = dict(b)
            b["name"] = name
            out.append(b)
            has_tool = True
        elif t == "text":
            text = b.get("text", "")
            pos, converted = 0, False
            for m in CALL_RE.finditer(text):
                name = m.group(1)
                if name not in emulated_names:
                    continue
                try:
                    inp = json.loads(m.group(2).strip())
                except Exception:
                    log(f"BAD JSON in tool_call {name}: {head(m.group(2), 200)}")
                    continue
                if not isinstance(inp, dict):
                    continue
                pre = text[pos:m.start()]
                if pre.strip():
                    out.append({"type": "text", "text": pre})
                out.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24],
                            "name": name, "input": inp})
                has_tool = True
                converted = True
                pos = m.end()
            if not converted:
                out.append(b)  # no tool call found, keep text as-is
            # when converted, drop text after the last tool_call (model-guessed results)
    if not out:
        out = [{"type": "text", "text": " "}]
    return out, has_tool


def call_upstream(payload, version):
    headers = {"x-api-key": KEY, "anthropic-version": version, "content-type": "application/json"}
    return requests.post(f"{TARGET}/v1/messages", json=payload, headers=headers, timeout=600)


def est_tokens(obj):
    """Rough token estimate for a request body (chars/4 is within ~30% for code/JSON)."""
    try:
        return len(json.dumps(obj, ensure_ascii=False)) // 4
    except (TypeError, ValueError):
        return 0


def sanitize_usage(usage, payload):
    """Rewrite relay usage when the input side is implausibly inflated.

    The relay pads input counts (a 6-char user message reports ~20k input
    tokens); clients read usage as context occupancy, so one inflated reply
    freezes the session. Estimating from the actual payload keeps truthful
    usage intact (no rewrite when reported <= estimate * band + margin).
    """
    if not isinstance(usage, dict):
        return usage
    try:
        rep_in = sum(int(usage.get(k) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    except (TypeError, ValueError):
        return usage
    if rep_in == 0:
        return usage
    est = est_tokens(payload)
    if rep_in <= est * USAGE_BAND + USAGE_MARGIN:
        return usage
    fixed = dict(usage)
    fixed["input_tokens"] = min(usage.get("input_tokens") or 0, est)
    fixed["cache_creation_input_tokens"] = 0
    fixed["cache_read_input_tokens"] = max(0, est - fixed["input_tokens"])
    log(f"usage rewritten : reported input={rep_in} > est={est}*{USAGE_BAND}+{USAGE_MARGIN}")
    return fixed


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "claude-justdowork-recovery-proxy", "version": "1.0.0"}


@app.route("/v1/messages", methods=["POST"])
def proxy():
    counter["n"] += 1
    n = counter["n"]
    body = request.get_json(silent=True) or {}
    wants_stream = bool(body.get("stream"))
    version = request.headers.get("anthropic-version", "2023-06-01")

    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    emulated_names = {t.get("name") for t in tools
                      if "input_schema" in t and t.get("name") not in NAME_MAP}
    log(f"===== REQUEST #{n} | messages={len(body.get('messages', []))} =====")
    log(f"native tools   : {[t.get('name') for t in tools if t.get('name') in NAME_MAP]}")
    log(f"emulated tools : {sorted(emulated_names)}")
    dump_json(f"req_{n}.json", body)

    r = None
    for attempt, aggressive in enumerate((False, True), start=1):
        payload = sanitize(body, aggressive=aggressive)
        try:
            r = call_upstream(payload, version)
        except Exception as e:
            log(f"attempt {attempt} (aggressive={aggressive}) EXCEPTION: {e}")
            continue
        log(f"attempt {attempt} (aggressive={aggressive}) -> status {r.status_code}")
        if r.status_code == 200:
            break
        log(f"upstream error body: {r.text[:600]}")
        dump_json(f"fail_{n}_attempt{attempt}.json", payload)

    if r is None or r.status_code != 200:
        text = r.text if r is not None else json.dumps({"error": {"message": "proxy failed"}})
        return Response(text, status=(r.status_code if r is not None else 500),
                        content_type="application/json")

    msg = r.json()
    blocks, has_tool = parse_response(msg, emulated_names)
    msg["content"] = blocks
    msg["stop_reason"] = "tool_use" if has_tool else (msg.get("stop_reason") or "end_turn")
    msg["usage"] = sanitize_usage(msg.get("usage"), payload)
    log(f"relay blocks   : {[b.get('type') for b in r.json().get('content', [])]}")
    for b in blocks:
        if b.get("type") == "tool_use":
            log(f"TOOL_USE       : {b.get('name')} {head(b.get('input'), 200)}")
        else:
            log(f"text           : {head(b.get('text', ''), 200)}")
    log(f"stop_reason    : {msg['stop_reason']}")

    if not wants_stream:
        return Response(json.dumps(msg), status=200, content_type="application/json")

    def gen():
        usage = msg.get("usage", {})
        start = dict(msg)
        start["content"] = []
        start["stop_reason"] = None
        start["usage"] = {k: usage[k] for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens") if k in usage}
        start["usage"]["output_tokens"] = 0
        yield sse("message_start", {"type": "message_start", "message": start})
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                yield sse("content_block_start", {"type": "content_block_start", "index": i,
                          "content_block": {"type": "text", "text": ""}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "text_delta", "text": b.get("text", "")}})
            else:
                yield sse("content_block_start", {"type": "content_block_start", "index": i,
                          "content_block": {"type": "tool_use", "id": b.get("id"),
                                            "name": b.get("name"), "input": {}}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "input_json_delta",
                                    "partial_json": json.dumps(b.get("input", {}))}})
            yield sse("content_block_stop", {"type": "content_block_stop", "index": i})
        yield sse("message_delta", {"type": "message_delta",
                  "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                  "usage": {"output_tokens": usage.get("output_tokens", 0)}})
        yield sse("message_stop", {"type": "message_stop"})

    return Response(stream_with_context(gen()), status=200,
                    content_type="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    if not KEY:
        raise SystemExit("Set UPSTREAM_API_KEY first: export UPSTREAM_API_KEY='...'")
    open(LOG, "w").close()
    log(f"Claude JustDoWork Recovery Proxy v1.0 on http://127.0.0.1:{PORT} -> {TARGET}")
    log(f"native map: {NAME_MAP}")
    log(f"debug dumps: {'ON -> ' + DUMP_DIR + '/ (contains prompts + file contents)' if DUMP else 'off (set DEBUG_DUMP=1 to enable)'}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
#!/usr/bin/env python3
"""Claude Code giả lập cho kiểm thử PD_Bridge (không tốn token).

Hành vi lấy lần lượt từ $PD_BRIDGE_HOME/fake_script.json (list), mặc định "ok":
  ok          ghi answer.md theo đường dẫn trong prompt
  ok_text     không ghi file, trả câu trả lời trong "result"
  limit_opus  nếu model là opus -> lỗi hạn mức; nếu không -> ok
  limit_all   lỗi hạn mức (reset sau 2 giờ)
  login       lỗi chưa đăng nhập
  max_turns   hết lượt, không ghi file (lần resume sau sẽ ghi)
  error       lỗi bất kỳ
  weekly      ghi file tổng hợp tuần + cập nhật kinh_nghiem.md
Mọi lần gọi được ghi vào $PD_BRIDGE_HOME/fake_calls.jsonl
"""
import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("PD_BRIDGE_HOME", "."))
args = sys.argv[1:]

if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:1] == ["--help"]:
    print("  -p, --print  --output-format <format>  --model <model>  --permission-mode <mode>\n"
          "  --allowedTools <tools...>  --disallowedTools <tools...>  --add-dir <directories...>\n"
          "  --effort <level>  --fallback-model <model>  --strict-mcp-config  --resume\n"
          "  --tools <tools...>  --exclude-dynamic-system-prompt-sections  --verbose")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    st = (HOME / "fake_auth.json")
    print(st.read_text() if st.exists() else json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    sys.exit(0)

if args[:1] == ["update"]:
    print("Claude Code is up to date")
    sys.exit(0)
prompt = sys.stdin.read()
model = args[args.index("--model") + 1] if "--model" in args else "?"
resume = "--resume" in args
script_p = HOME / "fake_script.json"
script = json.loads(script_p.read_text()) if script_p.exists() else []
beh = script.pop(0) if script else "ok"
script_p.write_text(json.dumps(script))
with open(HOME / "fake_calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"beh": beh, "model": model, "args": args, "resume": resume,
                        "cwd": os.getcwd(), "api_key": os.environ.get("ANTHROPIC_API_KEY"),
                        "prompt": prompt}, ensure_ascii=False) + "\n")

full = {"claude-opus": "claude-opus-4-7", "opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6"}.get(model, model)


def result(text, sub="success", err=False, turns=5):
    print(json.dumps({"type": "result", "subtype": sub, "is_error": err, "result": text,
                      "session_id": "sess-123", "num_turns": turns, "total_cost_usd": 0.1,
                      "modelUsage": {full: {"outputTokens": 100}}}))


m = re.search(r"vào file: (.+?)\n", prompt)
target = Path(m.group(1).strip()) if m else None
if beh == "limit_opus" and "opus" in model:
    beh = "limit_all"
elif beh == "limit_opus":
    beh = "ok"

if "stream-json" in args:      # giả lập sự kiện tool_use của stream-json
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "uncertainty"}}]}}), flush=True)
if beh == "old_cli" and "--tools" in args:
    print("error: unknown option '--tools'", file=sys.stderr)
    sys.exit(1)
if beh == "old_cli":
    beh = "ok"
if beh == "wrong_place":         # ghi câu trả lời nhầm tên file trong run_dir, rồi in ngắn
    if target:
        (target.parent / "cau_tra_loi.md").write_text("## Tóm tắt\n\n" + "Ghi nhầm chỗ. " * 40, encoding="utf-8")
    result("XONG")
    sys.exit(0)
if beh == "no_file":             # quên ghi file; lần resume thì ghi
    if resume and target:
        target.write_text("## Tóm tắt\n\nGhi sau khi được nhắc. " + "y " * 200, encoding="utf-8")
    result("XONG")
    sys.exit(0)
if beh == "hang":
    time.sleep(30)
    sys.exit(0)
if beh == "slow_ok":
    time.sleep(1.5)
    beh = "ok"
if beh == "ok":
    q = re.search(r"<<<\n(.*?)\n>>>", prompt, re.S)
    body = ("## Tóm tắt\n\nUncertainty sau CTS nên giảm phần skew, giữ jitter + margin. "
            f"(trả lời giả lập cho: {q.group(1)[:80] if q else ''})\n\n## Trả lời chi tiết\n\n"
            + "Nội dung chi tiết. " * 30 + "\n\n## Nguồn\n\n- Trên máy: docs/innovus_tcr.pdf p.3\n\nXONG")
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    result("XONG")
elif beh == "ok_text":
    result("## Tóm tắt\n\nTrả lời qua result. " + "Chi tiết. " * 60)
elif beh == "limit_all":
    reset = int(time.time()) + 7200
    result(f"Claude AI usage limit reached|{reset}", sub="success", err=True, turns=0)
    sys.exit(1)
elif beh == "login":
    result("Invalid API key · Please run /login", err=True, turns=0)
    sys.exit(1)
elif beh == "max_turns":
    if resume and target:
        target.write_text("## Tóm tắt\n\nViết sau khi hết lượt. " + "x " * 200, encoding="utf-8")
        result("XONG")
    else:
        result("", sub="error_max_turns", err=True, turns=80)
        sys.exit(1)
elif beh == "weekly":
    m = re.search(r"Viết file (.+?\.md)", prompt)
    Path(m.group(1)).write_text("# Tổng hợp tuần (giả lập)\n", encoding="utf-8")
    kn = re.search(r"Cập nhật (.+?kinh_nghiem\.md)", prompt)
    if kn:
        with open(kn.group(1), "a", encoding="utf-8") as f:
            f.write("\n- ✅ Sau CTS giảm uncertainty về jitter (Q001, tuần test)\n")
    result("XONG")
else:
    result("API Error: something bad happened", err=True)
    sys.exit(1)

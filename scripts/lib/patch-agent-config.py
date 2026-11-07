#!/usr/bin/env python3
"""Apply agent configuration inside the guest, reading secrets from files.

Run inside the guest. Expects:
    /tmp/.llmkey     the LLM API key            (0600)
    /tmp/.fssecret   the Feishu app secret      (0600, optional)

Environment:
    AGENTVM_LLM_BASE_URL    e.g. https://api.minimax.cn/v1
    AGENTVM_LLM_MODEL       e.g. MiniMax-M3.1-Flash
    AGENTVM_FEISHU_APP_ID   cli_...
    AGENTVM_FEISHU_DOMAIN   feishu | lark
    AGENTVM_YOLO            1 to bypass approval prompts, 0 to keep them

Writes ~/.hermes/.env and patches ~/.hermes/config.yaml. Both are backed up
first. Idempotent: safe to run repeatedly.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
ENV_FILE = HERMES_HOME / ".env"
CONFIG_FILE = HERMES_HOME / "config.yaml"

STAMP = "2026-11-07"


def read_secret(path: str, min_len: int) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    val = p.read_text().strip()
    if len(val) < min_len:
        sys.exit(f"error: {path} is only {len(val)} bytes; refusing to use it")
    return val


def set_env(env_text: str, key: str, value: str) -> str:
    """Replace or append a KEY=value line, preserving the rest of the file."""
    pat = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    if pat.search(env_text):
        return pat.sub(f"{key}={value}", env_text)
    if env_text and not env_text.endswith("\n"):
        env_text += "\n"
    return env_text + f"{key}={value}\n"


def main() -> int:
    if not HERMES_HOME.is_dir():
        sys.exit(f"error: {HERMES_HOME} not found — install the agent first")

    llm_key = read_secret("/tmp/.llmkey", 20)
    if not llm_key:
        sys.exit("error: /tmp/.llmkey missing")

    base_url = os.environ.get("AGENTVM_LLM_BASE_URL", "").strip()
    model = os.environ.get("AGENTVM_LLM_MODEL", "").strip()
    feishu_id = os.environ.get("AGENTVM_FEISHU_APP_ID", "").strip()
    feishu_domain = os.environ.get("AGENTVM_FEISHU_DOMAIN", "feishu").strip() or "feishu"
    yolo = os.environ.get("AGENTVM_YOLO", "0") == "1"

    # ------------------------------------------------------------------ .env --
    if ENV_FILE.exists():
        shutil.copy(ENV_FILE, ENV_FILE.with_suffix(f".env.bak-{STAMP}"))
        env = ENV_FILE.read_text(encoding="utf-8")
    else:
        env = "# Hermes environment\n"

    # Provider-agnostic: the key/base-url pair is what matters, the variable
    # names follow whatever provider the agent build expects. Adjust here if
    # your build uses different names.
    env = set_env(env, "MINIMAX_CN_API_KEY", llm_key)
    if base_url:
        env = set_env(env, "MINIMAX_CN_BASE_URL", base_url)

    fs_secret = read_secret("/tmp/.fssecret", 16)
    if feishu_id:
        env = set_env(env, "FEISHU_APP_ID", feishu_id)
        env = set_env(env, "FEISHU_DOMAIN", feishu_domain)
        # websocket = no public URL needed, works behind NAT. This is the default
        # but set it explicitly so a future provider default change can't bite.
        env = set_env(env, "FEISHU_CONNECTION_MODE", "websocket")
    if fs_secret:
        env = set_env(env, "FEISHU_APP_SECRET", fs_secret)
    if yolo:
        env = set_env(env, "HERMES_YOLO_MODE", "1")

    ENV_FILE.write_text(env, encoding="utf-8")
    ENV_FILE.chmod(0o600)
    print(f"ok  wrote {ENV_FILE} (0600)")

    # ----------------------------------------------------------- config.yaml --
    if not CONFIG_FILE.exists():
        print(f"warn  {CONFIG_FILE} not found; skipping model config")
    else:
        shutil.copy(CONFIG_FILE, CONFIG_FILE.with_suffix(f".yaml.bak-{STAMP}"))
        text = CONFIG_FILE.read_text(encoding="utf-8")
        m = re.search(r"(?m)^model:\n(?:[ \t]+.*\n)*", text)
        if not m:
            print("warn  no model: section in config.yaml; skipping")
        else:
            block = m.group(0)
            if model:
                block = re.sub(r'(?m)^(\s*default:\s*)"[^"]*"', r'\1"%s"' % model, block, count=1)
            if base_url:
                block = re.sub(r'(?m)^(\s*base_url:\s*)"[^"]*"', r'\1"%s"' % base_url, block, count=1)
            text = text[: m.start()] + block + text[m.end():]
            print("ok  patched model section")

        if yolo:
            # approvals.mode: "off" is read at call time, unlike HERMES_YOLO_MODE
            # which is frozen at import. Belt and braces: set both.
            am = re.search(r"(?m)^approvals:\n", text)
            if am and not re.search(r"(?m)^\s+mode:\s*", text[am.end():am.end() + 400]):
                text = text[: am.end()] + '  mode: "off"\n' + text[am.end():]
                print("ok  set approvals.mode: off")

        CONFIG_FILE.write_text(text, encoding="utf-8")
        print(f"ok  wrote {CONFIG_FILE}")

    # Clean up the plaintext secrets the moment they are in place.
    for tmp in ("/tmp/.llmkey", "/tmp/.fssecret"):
        try:
            os.unlink(tmp)
            print(f"ok  removed {tmp}")
        except FileNotFoundError:
            pass

    print("\nVerify with:")
    print("  hermes status")
    print("  hermes -z 'reply with exactly: PONG'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

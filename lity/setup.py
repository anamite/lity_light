"""Interactive setup wizard + tiny settings CLI.

    python -m lity.setup                # wizard: provider, models, keys, voice…
    python -m lity.setup show           # effective config + which keys are set
    python -m lity.setup get [a.b]      # read one value (or a whole section)
    python -m lity.setup set a.b VALUE  # write one config.yaml value
    python -m lity.setup key NAME [VAL] # set a .env key (no VAL = hidden prompt)

(usually via ./lityctl setup / show / get / set / key)

config.yaml is edited LINE-BASED so every comment survives; .env is rewritten
key-by-key the same way. Values: true/false/numbers parse as YAML scalars."""

import argparse
import getpass
import re
import sys
from pathlib import Path

import yaml

ROOT = Path.cwd()
CONFIG = ROOT / "config.yaml"
ENV = ROOT / ".env"

PROVIDERS = {
    "1": ("OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "2": ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "3": ("Ollama (local, no key)", "http://localhost:11434/v1", "OLLAMA_API_KEY"),
}


# ── .env editing (comment/order preserving) ─────────────────────────────────
def env_read() -> dict[str, str]:
    out = {}
    if ENV.is_file():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def env_set(name: str, value: str):
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.is_file() else []
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(name)}\s*=", line):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask(secret: str) -> str:
    if not secret:
        return "(not set)"
    return secret[:4] + "..." + secret[-4:] if len(secret) > 10 else "****"


# ── config.yaml editing (comment-preserving, max 2 levels deep) ─────────────
def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    v = str(value)
    return f'"{v}"' if re.search(r"[#:{}\[\],&*?|>'\"%@`]", v) else v


def _parse(token: str):
    try:
        return yaml.safe_load(token)
    except yaml.YAMLError:
        return token


def config_set(dotted: str, value):
    """Set `section.key` (or top-level `key`) in config.yaml, keeping every
    comment and the file's ordering intact."""
    text = CONFIG.read_text(encoding="utf-8")
    lines = text.splitlines()
    section, _, key = dotted.partition(".")
    new = _fmt(value)

    def replace(i: int, indent: str, name: str) -> None:
        m = re.match(rf"^{re.escape(indent)}{re.escape(name)}:\s*([^#]*?)(\s*#.*)?$",
                     lines[i])
        comment = f"  {m.group(2).strip()}" if m.group(2) else ""
        lines[i] = f"{indent}{name}: {new}".rstrip() + comment

    if not key:  # top-level scalar, e.g. autonomy_level
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(section)}:", line):
                replace(i, "", section)
                break
        else:
            lines += [f"{section}: {new}"]
    else:
        sec_i = next((i for i, l in enumerate(lines)
                      if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l)), None)
        if sec_i is None:
            lines += ["", f"{section}:", f"  {key}: {new}"]
        else:
            end = next((i for i in range(sec_i + 1, len(lines))
                        if lines[i].strip() and not lines[i].startswith((" ", "\t"))),
                       len(lines))
            for i in range(sec_i + 1, end):
                if re.match(rf"^\s+{re.escape(key)}:", lines[i]):
                    indent = re.match(r"^(\s+)", lines[i]).group(1)
                    replace(i, indent, key)
                    break
            else:
                lines.insert(sec_i + 1, f"  {key}: {new}")
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def config_get(dotted: str = ""):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    if not dotted:
        return data
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ── telegram pairing ────────────────────────────────────────────────────────
def telegram_pair(token: str, wait_seconds: int = 60):
    """Wait for the user to message their new bot; return the chat dict."""
    import time

    import httpx
    base = f"https://api.telegram.org/bot{token}"
    deadline = time.time() + wait_seconds
    offset = None
    while time.time() < deadline:
        try:
            params = {"timeout": 10}
            if offset is not None:
                params["offset"] = offset
            r = httpx.get(f"{base}/getUpdates", params=params, timeout=15)
            data = r.json()
        except Exception:
            time.sleep(2)
            continue
        if not data.get("ok"):
            print(f"     Telegram says: {data.get('description', 'error')} — check the token.")
            return None
        for u in data.get("result", []):
            offset = u.get("update_id", 0) + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                return chat
    return None


# ── interactive helpers ─────────────────────────────────────────────────────
def ask(prompt: str, current=None) -> str | None:
    """Prompt; empty answer (or EOF / piped end) keeps the current value."""
    shown = "" if current in (None, "") else f" [{current}]"
    try:
        val = input(f"  {prompt}{shown}: ").strip()
    except EOFError:
        return None
    return val or None


def ask_secret(prompt: str, current: str) -> str | None:
    text = f"  {prompt} [{mask(current)}] (enter=keep): "
    try:
        # getpass reads the console directly on Windows and would hang on
        # piped input; visible input() is fine for non-interactive runs.
        val = (getpass.getpass(text) if sys.stdin.isatty() else input(text)).strip()
    except EOFError:
        return None
    return val or None


def ask_yesno(prompt: str, current: bool) -> bool:
    cur = "Y/n" if current else "y/N"
    try:
        val = input(f"  {prompt} [{cur}]: ").strip().lower()
    except EOFError:
        return current
    if val in ("y", "yes"):
        return True
    if val in ("n", "no"):
        return False
    return current


# ── the wizard ──────────────────────────────────────────────────────────────
def wizard():
    if not CONFIG.is_file():
        print("config.yaml not found — run this from the Lity directory.")
        sys.exit(1)
    ENV.touch(exist_ok=True)
    env = env_read()

    print("── Lity setup ──────────────────────────────────────────")
    print("Enter = keep the current value. Keys are typed hidden.\n")

    # 1. provider ------------------------------------------------------------
    cur_url = config_get("provider.base_url")
    cur_env = config_get("provider.api_key_env") or "OPENAI_API_KEY"
    print(f"1) Model provider   (current: {cur_url})")
    for n, (label, url, _) in PROVIDERS.items():
        print(f"     [{n}] {label} — {url}")
    print("     [4] custom base_url")
    choice = ask("provider")
    if choice in PROVIDERS:
        _, url, key_env = PROVIDERS[choice]
        config_set("provider.base_url", url)
        config_set("provider.api_key_env", key_env)
        cur_url, cur_env = url, key_env
    elif choice == "4":
        url = ask("base_url (…/v1)")
        if url:
            config_set("provider.base_url", url)
            cur_url = url
        key_env = ask("env var name for its API key", cur_env)
        if key_env:
            config_set("provider.api_key_env", key_env.upper())
            cur_env = key_env.upper()
    if "ollama" not in (cur_url or "").lower():
        val = ask_secret(f"{cur_env}", env.get(cur_env, ""))
        if val:
            env_set(cur_env, val)
    else:
        print("     (local Ollama needs no API key)")

    # 2. models ---------------------------------------------------------------
    print("\n2) Models (any id your provider serves)")
    for slot in ("main", "utility", "vision"):
        val = ask(f"models.{slot}", config_get(f"models.{slot}"))
        if val:
            config_set(f"models.{slot}", val)

    # 3. hermes ---------------------------------------------------------------
    print("\n3) Hermes executor (runs all delegated tasks)")
    en = ask_yesno("enable Hermes", bool(config_get("hermes.enabled")))
    config_set("hermes.enabled", en)
    if en:
        val = ask("hermes.base_url", config_get("hermes.base_url"))
        if val:
            config_set("hermes.base_url", val)
        val = ask_secret("HERMES_API_KEY", env.get("HERMES_API_KEY", ""))
        if val:
            env_set("HERMES_API_KEY", val)

    # 4. voice ----------------------------------------------------------------
    print("\n4) Voice assistant (wake word + mic/speaker, runs in-process)")
    en = ask_yesno("enable voice on every start", bool(config_get("voice.enabled")))
    config_set("voice.enabled", en)
    if en or ask_yesno("configure voice anyway (for --voice runs)", False):
        val = ask("wake word (hey_jarvis / alexa / hey_mycroft / hey_rhasspy)",
                  config_get("voice.wake_word"))
        if val:
            config_set("voice.wake_word", val)
        val = ask_secret("SPEECHMATICS_API_KEY", env.get("SPEECHMATICS_API_KEY", ""))
        if val:
            env_set("SPEECHMATICS_API_KEY", val)

        cur_tts = config_get("voice.tts_engine") or "kokoro"
        print(f"   TTS engine        (current: {cur_tts})")
        print("     [1] kokoro   — local, free, no key (slower on a Pi)")
        print("     [2] resemble — Resemble AI cloud (Chatterbox voices), fast, needs API key")
        print("     [3] openai   — OpenAI gpt-4o-mini-tts, fast, uses OPENAI_API_KEY")
        choice = ask("tts engine")
        engine = {"1": "kokoro", "2": "resemble", "3": "openai"}.get(choice or "", cur_tts)
        config_set("voice.tts_engine", engine)
        if engine == "kokoro":
            val = ask("Kokoro voice id", config_get("voice.tts_voice") or "af_heart")
            if val:
                config_set("voice.tts_voice", val)
        elif engine == "resemble":
            val = ask("Resemble voice UUID (app.resemble.ai/hub/voices)",
                      config_get("voice.resemble_voice"))
            if val:
                config_set("voice.resemble_voice", val)
            val = ask_secret("RESEMBLE_API_KEY", env.get("RESEMBLE_API_KEY", ""))
            if val:
                env_set("RESEMBLE_API_KEY", val)
        elif engine == "openai":
            val = ask("OpenAI TTS voice (alloy/ash/ballad/coral/echo/fable/nova/onyx/sage/shimmer)",
                      config_get("voice.openai_tts_voice") or "nova")
            if val:
                config_set("voice.openai_tts_voice", val)
            if not env.get("OPENAI_API_KEY"):
                val = ask_secret("OPENAI_API_KEY (needed for OpenAI TTS)",
                                 env.get("OPENAI_API_KEY", ""))
                if val:
                    env_set("OPENAI_API_KEY", val)

    # 5. optional bearer auth on /v1 -----------------------------------------
    print("\n5) Voice API auth (only needed if other machines call /v1)")
    val = ask_secret("LITY_API_KEY (empty = open on LAN)", env.get("LITY_API_KEY", ""))
    if val:
        env_set("LITY_API_KEY", val)

    # 6. external modules -----------------------------------------------------
    print("\n6) External modules")
    en = ask_yesno("enable Google Calendar module", bool(config_get("gcal.enabled")))
    config_set("gcal.enabled", en)
    if en:
        val = ask("calendar id (usually your gmail address)", config_get("gcal.calendar_id"))
        if val:
            config_set("gcal.calendar_id", val)
        cur = config_get("gcal.inject_daily") or "always"
        print(f"   daily agenda in the assistant's context (current: {cur})")
        print("     [1] always    — today's events visible on every turn")
        print("     [2] on_demand — fetched only when the calendar tool is used")
        choice = ask("inject_daily")
        if choice in ("1", "2"):
            config_set("gcal.inject_daily", "always" if choice == "1" else "on_demand")
        sa = config_get("gcal.service_account_file") or "data/gcal_service_account.json"
        print(f"""   Remaining one-time steps (or just ask Lity: "set up google calendar"):
     1. console.cloud.google.com → enable "Google Calendar API" → create a
        service account → Keys → new JSON key (downloads a .json file)
     2. put that file at: {sa}
     3. share your calendar (calendar.google.com → Settings) with the
        service account's client_email — permission "Make changes to events"
     4. deps once, in the venv: pip install -r requirements-modules.txt
   Config is re-read live — no restart needed.""")

    en = ask_yesno("enable Telegram bridge (messages, files, approval buttons)",
                   bool(config_get("telegram.enabled")))
    config_set("telegram.enabled", en)
    if en:
        print("   Create a bot once: message @BotFather in Telegram → /newbot → "
              "copy the token.")
        tok = ask_secret("TELEGRAM_BOT_TOKEN", env.get("TELEGRAM_BOT_TOKEN", ""))
        if tok:
            env_set("TELEGRAM_BOT_TOKEN", tok)
        tok = tok or env.get("TELEGRAM_BOT_TOKEN", "")
        cur_chat = config_get("telegram.chat_id")
        if tok and ask_yesno("pair your chat id automatically now", not cur_chat):
            print("     → Send ANY message to your bot in Telegram (waiting up to 60s)…")
            chat = telegram_pair(tok)
            if chat:
                config_set("telegram.chat_id", str(chat["id"]))
                who = chat.get("first_name") or chat.get("username") or chat["id"]
                print(f"     Paired with {who} (chat id {chat['id']}).")
            else:
                print("     No message arrived — set it later with "
                      "./lityctl set telegram.chat_id YOUR_ID")
        else:
            val = ask("telegram.chat_id (from @userinfobot)", cur_chat)
            if val:
                config_set("telegram.chat_id", val)
        print("   Approval requests will show up there with decision buttons "
              "(disable: ./lityctl set telegram.forward_approvals false).")

    print("\n── saved. Review anytime with:  ./lityctl show")
    show()


# ── show ────────────────────────────────────────────────────────────────────
def show():
    env = env_read()
    cfg = config_get()
    key_env = (cfg.get("provider") or {}).get("api_key_env", "OPENAI_API_KEY")
    print("\nprovider :", (cfg.get("provider") or {}).get("base_url"))
    print("models   :", ", ".join(f"{k}={v}" for k, v in (cfg.get("models") or {}).items()))
    print("hermes   :", "enabled -> " + str((cfg.get("hermes") or {}).get("base_url"))
          if (cfg.get("hermes") or {}).get("enabled") else "disabled")
    v = cfg.get("voice") or {}
    print("voice    :", (f"enabled (wake: {v.get('wake_word')}, "
                         f"tts: {v.get('tts_engine') or 'kokoro'})" if v.get("enabled")
                         else f"disabled (enable: ./lityctl start --voice)"))
    g = cfg.get("gcal") or {}
    if g.get("enabled"):
        key_file = ROOT / str(g.get("service_account_file") or "data/gcal_service_account.json")
        print("gcal     :", f"enabled (calendar: {g.get('calendar_id') or 'NOT SET'}, "
              f"key file: {'found' if key_file.is_file() else 'MISSING'}, "
              f"agenda: {g.get('inject_daily') or 'always'})")
    else:
        print("gcal     : disabled (module — enable in setup step 6, or ask Lity)")
    t = cfg.get("telegram") or {}
    if t.get("enabled"):
        print("telegram :", f"enabled (chat: {t.get('chat_id') or 'NOT PAIRED'}, "
              f"token: {'set' if env.get('TELEGRAM_BOT_TOKEN') else 'MISSING'}, "
              f"approval buttons: {'on' if t.get('forward_approvals', True) else 'off'})")
    else:
        print("telegram : disabled (module — enable in setup step 6, or ask Lity)")
    print("keys     :")
    for name in dict.fromkeys([key_env, "HERMES_API_KEY", "SPEECHMATICS_API_KEY",
                               "RESEMBLE_API_KEY", "TELEGRAM_BOT_TOKEN", "LITY_API_KEY"]):
        print(f"  {name:<22} {mask(env.get(name, ''))}")


def main():
    # Windows pipes default to cp1252, which chokes on the box-drawing chars.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="lity-setup", description=__doc__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("wizard")
    sub.add_parser("show")
    g = sub.add_parser("get")
    g.add_argument("path", nargs="?", default="")
    s = sub.add_parser("set")
    s.add_argument("path")
    s.add_argument("value")
    k = sub.add_parser("key")
    k.add_argument("name")
    k.add_argument("value", nargs="?")
    args = p.parse_args()

    if args.cmd in (None, "wizard"):
        wizard()
    elif args.cmd == "show":
        show()
    elif args.cmd == "get":
        print(yaml.safe_dump(config_get(args.path), default_flow_style=False,
                             allow_unicode=True).strip())
    elif args.cmd == "set":
        config_set(args.path, _parse(args.value))
        print(f"{args.path} = {_parse(args.value)!r}  (config.yaml updated)")
    elif args.cmd == "key":
        val = args.value or getpass.getpass(f"{args.name}: ").strip()
        env_set(args.name, val)
        print(f"{args.name} saved to .env ({mask(val)})")


if __name__ == "__main__":
    main()

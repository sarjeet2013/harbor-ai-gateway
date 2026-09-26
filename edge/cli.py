"""CLI: python -m edge {start|stop|restart|status|config-check|cache-stats|doctor|models} (+ Admin UI at /admin)"""

from __future__ import annotations

import argparse
import webbrowser
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .deps import (
    ensure_deps,
    ensure_upstream_deps,
    format_litellm_missing_message,
    litellm_ready,
    preflight_ok_message,
    resolve_litellm_cmd,
)
from .config import GatewayConfig, load_config, repo_root
from .litellm_gen import resolved_aliases, write_litellm_config

PID_PATH = repo_root() / "data" / "gateway.pid"
EDGE_LOG = repo_root() / "logs" / "edge.log"
LITELLM_LOG = repo_root() / "logs" / "litellm.log"


def _ensure_dirs() -> None:
    (repo_root() / "data").mkdir(parents=True, exist_ok=True)
    (repo_root() / "logs").mkdir(parents=True, exist_ok=True)


def _load_dotenv(path: Path | None = None) -> list[str]:
    """Load KEY=VAL from repo .env into os.environ (do not override existing).

    Returns list of keys newly set. Shell exports always win.
    Blank assignments (``KEY=``) are skipped so a later non-empty line can load.
    """
    env_path = path or (repo_root() / ".env")
    if not env_path.is_file():
        return []
    loaded: list[str] = []
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if not val:
            continue  # blank KEY= — allow a later non-empty line
        os.environ[key] = val
        loaded.append(key)
    return loaded


def _cloud_key_status(cfg: GatewayConfig) -> None:
    """Print whether cloud keys needed by configured models are present."""
    specs = [s for s in cfg.models.all_specs() if s.litellm_model]
    needs_openai = any((s.litellm_model or "").startswith("openai/") for s in specs)
    needs_xai = any((s.litellm_model or "").startswith("xai/") for s in specs)
    if needs_openai:
        k = os.environ.get("OPENAI_API_KEY")
        if k:
            print(f"OPENAI_API_KEY: set (len={len(k)})")
        else:
            print(
                "OPENAI_API_KEY: UNSET — expensive/OpenAI calls will fail until "
                "you export it or put it in .env",
                file=sys.stderr,
            )
    if needs_xai:
        k = os.environ.get("XAI_API_KEY")
        print(f"XAI_API_KEY: {'set (len=%d)' % len(k) if k else 'UNSET'}")





def _ensure_ssl_ca_env(env: dict[str, str] | None = None) -> str | None:
    """Point SSL at certifi CAs when unset.

    LiteLLM proxy uses aiohttp for cloud providers. On many macOS Python builds the
    default trust store cannot verify public CAs, so OpenAI calls fail as
    ``CERTIFICATE_VERIFY_FAILED`` and LiteLLM surfaces a vague "Connection error".
    httpx (and Harbor's own probes) already use certifi; aiohttp needs
    ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``.
    """
    target: dict[str, str] = env if env is not None else os.environ  # type: ignore[assignment]
    existing = target.get("SSL_CERT_FILE") or target.get("REQUESTS_CA_BUNDLE")
    if existing:
        return existing
    try:
        import certifi
    except ImportError:
        return None
    ca = certifi.where()
    if not Path(ca).is_file():
        return None
    target.setdefault("SSL_CERT_FILE", ca)
    target.setdefault("REQUESTS_CA_BUNDLE", ca)
    return ca


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    # Treat 0.0.0.0 as localhost for connect checks
    check_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((check_host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pidfile() -> dict[str, Any] | None:
    if not PID_PATH.exists():
        return None
    try:
        return json.loads(PID_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_pidfile(data: dict[str, Any]) -> None:
    _ensure_dirs()
    PID_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _is_ollama_model(litellm_model: str | None) -> bool:
    """True for local Ollama prefixes (ollama/ and ollama_chat/)."""
    if not litellm_model:
        return False
    return litellm_model.startswith("ollama/") or litellm_model.startswith("ollama_chat/")


def _warn_cloud_keys(cfg: GatewayConfig) -> None:
    specs = cfg.models.all_specs()
    if not specs:
        return
    with_model = [s for s in specs if s.litellm_model]
    # Warn (do not fail start) when cloud models lack their env keys.
    if any((s.litellm_model or "").startswith("openai/") for s in with_model):
        if not os.environ.get("OPENAI_API_KEY"):
            print(
                "warning: OPENAI_API_KEY is unset but config has openai/* model(s) — "
                "cheap/Ollama still works; expensive/OpenAI requests will fail until "
                "you export OPENAI_API_KEY.",
                file=sys.stderr,
            )
    if any((s.litellm_model or "").startswith("xai/") for s in with_model):
        if not os.environ.get("XAI_API_KEY"):
            print(
                "warning: XAI_API_KEY is unset but config has xai/* model(s) — "
                "cheap/Ollama still works; expensive/Grok requests will fail until "
                "you export XAI_API_KEY.",
                file=sys.stderr,
            )
    # If every configured model is local Ollama, cloud OpenAI/Anthropic keys are unused.
    if with_model and all(_is_ollama_model(s.litellm_model) for s in with_model):
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_API_KEY"):
            if os.environ.get(key):
                print(
                    f"warning: {key} is set but all models are ollama/* / ollama_chat/* — "
                    "Ollama does not need cloud keys; ignore or unset if unintended.",
                    file=sys.stderr,
                )


def _find_litellm_cmd() -> list[str]:
    # Always same-interpreter: python -m edge.litellm_cli (see edge.deps).
    cmd = resolve_litellm_cmd()
    if cmd is None:
        raise SystemExit(format_litellm_missing_message())
    return cmd


def _start_litellm(cfg: GatewayConfig, litellm_cfg: Path, *, detach: bool) -> subprocess.Popen:
    cmd = _find_litellm_cmd() + [
        "--config",
        str(litellm_cfg),
        "--host",
        cfg.litellm_host,
        "--port",
        str(cfg.litellm_port),
    ]
    LITELLM_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_f = LITELLM_LOG.open("a")
    # Inherit current env explicitly so OPENAI_API_KEY / .env loads reach LiteLLM.
    # Pin certifi CAs for aiohttp (LiteLLM OpenAI path) — see _ensure_ssl_ca_env.
    child_env = dict(os.environ)
    _ensure_ssl_ca_env(child_env)
    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo_root()),
        "stdout": log_f,
        "stderr": subprocess.STDOUT,
        "start_new_session": True,  # own process group for clean kill
        "env": child_env,
    }
    try:
        return subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError as e:
        raise SystemExit(
            "Failed to launch LiteLLM with this interpreter.\n"
            f"  tried: {cmd!r}\n"
            "Install / repair with:\n"
            "  pip install -r requirements-litellm.txt\n"
            "  # or: pip install --force-reinstall 'litellm[proxy]'\n"
            f"detail: {e}"
        ) from e


def _start_edge(cfg: GatewayConfig, *, detach: bool) -> subprocess.Popen:
    host = os.environ.get("GATEWAY_HOST", cfg.host)
    port = int(os.environ.get("GATEWAY_PORT", str(cfg.port)))
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "edge.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    EDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_f = EDGE_LOG.open("a")
    return subprocess.Popen(
        cmd,
        cwd=str(repo_root()),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "GATEWAY_CONFIG": os.environ.get(
            "GATEWAY_CONFIG", str(repo_root() / "config" / "gateway.yaml")
        )},
    )


def _kill_pid(pid: int, *, grace: float = 2.0) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
    deadline = time.time() + grace
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def cmd_stop(_args: argparse.Namespace | None = None) -> int:
    info = _read_pidfile()
    stopped: list[str] = []
    if info:
        for key in ("litellm_pid", "edge_pid"):
            pid = int(info.get(key) or 0)
            if pid and _pid_alive(pid):
                _kill_pid(pid)
                stopped.append(f"{key}={pid}")
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    # Also try freeing known ports from config if pidfile missing
    cfg = load_config()
    for label, host, port in (
        ("edge", cfg.host, cfg.port),
        ("litellm", cfg.litellm_host, cfg.litellm_port),
    ):
        if _port_open(host, port):
            # Best-effort: use fuser/lsof if available
            _free_port(port)
            stopped.append(f"port:{label}:{port}")
    if stopped:
        print("stopped:", ", ".join(stopped))
    else:
        print("nothing running (no live pids/ports)")
    return 0


def _free_port(port: int) -> None:
    from shutil import which

    fuser = which("fuser")
    if fuser:
        subprocess.run(
            [fuser, "-k", f"{port}/tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    # macOS / fallback: lsof
    lsof = which("lsof")
    if not lsof:
        return
    try:
        out = subprocess.check_output(
            [lsof, "-ti", f"tcp:{port}"], text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            _kill_pid(int(line))


def cmd_status(_args: argparse.Namespace | None = None) -> int:
    ensure_deps()
    cfg = load_config()
    info = _read_pidfile() or {}
    edge_pid = int(info.get("edge_pid") or 0)
    litellm_pid = int(info.get("litellm_pid") or 0)
    edge_alive = _pid_alive(edge_pid) if edge_pid else _port_open(cfg.host, cfg.port)
    litellm_alive = (
        _pid_alive(litellm_pid)
        if litellm_pid
        else (
            _port_open(cfg.litellm_host, cfg.litellm_port)
            if cfg.upstream_mode == "litellm"
            else False
        )
    )
    print(f"upstream_mode: {cfg.upstream_mode}")
    print(f"listen:        {cfg.listen}  ({'up' if edge_alive else 'down'})")
    if cfg.upstream_mode == "litellm":
        print(
            f"litellm:       {cfg.litellm_host}:{cfg.litellm_port}  "
            f"({'up' if litellm_alive else 'down'})"
        )
    else:
        print("litellm:       (not started in mock mode)")
    print(f"pidfile:       {PID_PATH}  ({'present' if info else 'absent'})")
    if edge_pid:
        print(f"edge_pid:      {edge_pid}  ({'alive' if _pid_alive(edge_pid) else 'dead'})")
    if litellm_pid:
        print(
            f"litellm_pid:   {litellm_pid}  "
            f"({'alive' if _pid_alive(litellm_pid) else 'dead'})"
        )
    print("models:")
    for row in resolved_aliases(cfg):
        lm = row["litellm_model"] or "(none — mock/legacy name only)"
        print(f"  [{row['pool']}] {row['alias']} → {lm}")
    return 0


def cmd_config_check(_args: argparse.Namespace | None = None) -> int:
    ensure_deps()
    cfg = load_config()
    rows = resolved_aliases(cfg)
    print(f"config: upstream_mode={cfg.upstream_mode} listen={cfg.listen}")
    print(f"litellm target: {cfg.litellm_base_url}")
    print("resolved model aliases (sent to LiteLLM as model=):")
    ok = True
    for row in rows:
        flag = "OK" if row["will_send_to_litellm"] or cfg.upstream_mode == "mock" else "MISSING litellm_model"
        if flag != "OK" and cfg.upstream_mode == "litellm":
            ok = False
        print(
            f"  [{row['pool']}] alias={row['alias']!r} "
            f"litellm_model={row['litellm_model']!r} "
            f"api_base={row['api_base']!r}  [{flag}]"
        )
    # Also show what generation would produce
    from .litellm_gen import build_litellm_config

    built = build_litellm_config(cfg)
    names = [m["model_name"] for m in built["model_list"]]
    print(f"generated LiteLLM model_name list: {names}")
    if cfg.upstream_mode == "litellm" and not names:
        print("ERROR: no models with litellm_model — LiteLLM config would be empty", file=sys.stderr)
        ok = False
    return 0 if ok else 1



def _ensure_gateway_yaml() -> Path | None:
    """Ensure local ``config/gateway.yaml`` exists (no-op if ``GATEWAY_CONFIG`` set).

    On first run, copy ``config/gateway.yaml.example`` when present. Returns the
    destination path if it was seeded, else ``None``. Exits with a clear error
    when both the local config and the example template are missing.
    """
    if os.environ.get("GATEWAY_CONFIG"):
        return None
    path = repo_root() / "config" / "gateway.yaml"
    if path.exists():
        return None
    example = repo_root() / "config" / "gateway.yaml.example"
    # Prefer Docker-oriented template when present and we look like a container
    docker_example = repo_root() / "config" / "gateway.docker.yaml.example"
    if (
        Path("/.dockerenv").exists() or os.environ.get("HARBOR_DOCKER") == "1"
    ) and docker_example.is_file():
        example = docker_example
    if not example.is_file():
        print(
            f"missing {path} and no template at {example}.\n"
            "Create config/gateway.yaml (see README) or set GATEWAY_CONFIG.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example.read_text())
    print(f"seeded {path} from {example.name}")
    return path



def _maybe_open_admin(cfg: GatewayConfig, *, open_browser: bool) -> None:
    """Optionally open the mini Admin UI in the default browser (loopback URL)."""
    if not open_browser:
        return
    # Always advertise loopback — admin mutations are localhost-only.
    port = int(os.environ.get("GATEWAY_PORT", str(cfg.port)))
    url = f"http://127.0.0.1:{port}/admin"
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"warning: could not open browser ({e}); open {url} manually", file=sys.stderr)


def cmd_start(args: argparse.Namespace) -> int:
    ensure_deps()
    _ensure_gateway_yaml()
    loaded = _load_dotenv()
    ca = _ensure_ssl_ca_env()
    if ca:
        print(f"SSL CA: {ca}")
    if loaded:
        print(f"loaded .env keys: {', '.join(loaded)}")
    cfg = load_config()
    # Require litellm import + CLI *before* generating config / spawning
    ensure_upstream_deps(cfg.upstream_mode)
    _ensure_dirs()
    _cloud_key_status(cfg)
    _warn_cloud_keys(cfg)

    # Refuse double-start if our pidfile looks live
    existing = _read_pidfile()
    if existing:
        ep = int(existing.get("edge_pid") or 0)
        if ep and _pid_alive(ep):
            print(f"already running (edge_pid={ep}). Use: python -m edge stop", file=sys.stderr)
            return 1

    litellm_cfg_path: Path | None = None
    litellm_proc: subprocess.Popen | None = None
    edge_proc: subprocess.Popen | None = None

    if cfg.upstream_mode == "litellm":
        litellm_cfg_path = write_litellm_config(cfg)
        print(f"generated LiteLLM config: {litellm_cfg_path}")
        # Sanity: aliases present
        from .litellm_gen import build_litellm_config

        names = [m["model_name"] for m in build_litellm_config(cfg)["model_list"]]
        print(f"LiteLLM aliases: {names}")
        litellm_proc = _start_litellm(cfg, litellm_cfg_path, detach=args.detach)
        # Wait briefly for port
        for _ in range(50):
            if _port_open(cfg.litellm_host, cfg.litellm_port):
                break
            if litellm_proc.poll() is not None:
                print(
                    f"LiteLLM exited early (code={litellm_proc.returncode}). "
                    f"See {LITELLM_LOG}",
                    file=sys.stderr,
                )
                return 1
            time.sleep(0.1)
        else:
            print(
                f"warning: LiteLLM port {cfg.litellm_port} not open yet; continuing",
                file=sys.stderr,
            )
    else:
        print("upstream_mode=mock — not starting LiteLLM")

    edge_proc = _start_edge(cfg, detach=args.detach)
    for _ in range(50):
        if _port_open(cfg.host, cfg.port):
            break
        if edge_proc.poll() is not None:
            print(
                f"edge exited early (code={edge_proc.returncode}). See {EDGE_LOG}",
                file=sys.stderr,
            )
            if litellm_proc:
                _kill_pid(litellm_proc.pid)
            return 1
        time.sleep(0.1)

    pid_data = {
        "edge_pid": edge_proc.pid,
        "litellm_pid": litellm_proc.pid if litellm_proc else None,
        "listen": cfg.listen,
        "litellm_host": cfg.litellm_host,
        "litellm_port": cfg.litellm_port,
        "upstream_mode": cfg.upstream_mode,
        "litellm_config": str(litellm_cfg_path) if litellm_cfg_path else None,
    }
    _write_pidfile(pid_data)
    print(f"edge pid={edge_proc.pid}  log={EDGE_LOG}")
    if litellm_proc:
        print(f"litellm pid={litellm_proc.pid}  log={LITELLM_LOG}")
    print(f"pidfile={PID_PATH}")
    print(f"admin UI: http://127.0.0.1:{cfg.port}/admin")
    _maybe_open_admin(
        cfg,
        open_browser=bool(getattr(args, "open_browser", False) or cfg.admin.open_browser),
    )

    if args.detach:
        print("detached. stop with: python -m edge stop")
        return 0

    print("foreground — Ctrl+C to stop")

    # Track live child pids (may be replaced by Admin Apply → edge restart --detach).
    tracked = {
        "edge": edge_proc.pid,
        "litellm": litellm_proc.pid if litellm_proc else None,
    }

    def _shutdown(signum: int, _frame: Any) -> None:
        print(f"\nreceived signal {signum}, shutting down…")
        info = _read_pidfile() or {}
        pids = {
            int(info.get("edge_pid") or tracked["edge"] or 0),
            int(info.get("litellm_pid") or (tracked["litellm"] or 0) or 0),
            int(edge_proc.pid if edge_proc else 0),
            int(litellm_proc.pid if litellm_proc else 0),
        }
        for pid in pids:
            if pid:
                _kill_pid(pid)
        PID_PATH.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Wait on edge; reattach if Admin (or restart CLI) respawns children under a
    # new pidfile — needed so Docker PID 1 does not exit on Apply auto-restart.
    while True:
        info = _read_pidfile() or {}
        ep = int(info.get("edge_pid") or tracked["edge"] or 0)
        lp = int(info.get("litellm_pid") or (tracked["litellm"] or 0) or 0)
        if ep:
            tracked["edge"] = ep
        if lp:
            tracked["litellm"] = lp

        edge_alive = bool(ep and _pid_alive(ep))
        if not edge_alive:
            # Brief grace for restart helper to write a new pidfile + spawn
            time.sleep(1.0)
            info2 = _read_pidfile() or {}
            ep2 = int(info2.get("edge_pid") or 0)
            if ep2 and _pid_alive(ep2):
                tracked["edge"] = ep2
                lp2 = int(info2.get("litellm_pid") or 0)
                if lp2:
                    tracked["litellm"] = lp2
                print(f"reattached after restart (edge_pid={ep2})")
                continue
            print("edge exited")
            if tracked["litellm"]:
                _kill_pid(int(tracked["litellm"]))
            PID_PATH.unlink(missing_ok=True)
            # Prefer original Popen exit code when available
            code = edge_proc.poll()
            return code if code is not None else 0

        if cfg.upstream_mode == "litellm" and tracked["litellm"]:
            lp = int(tracked["litellm"])
            if not _pid_alive(lp):
                time.sleep(1.0)
                info2 = _read_pidfile() or {}
                lp2 = int(info2.get("litellm_pid") or 0)
                if lp2 and _pid_alive(lp2):
                    tracked["litellm"] = lp2
                    ep2 = int(info2.get("edge_pid") or 0)
                    if ep2:
                        tracked["edge"] = ep2
                    print(f"reattached LiteLLM after restart (litellm_pid={lp2})")
                    continue
                print(
                    f"LiteLLM exited; stopping edge",
                    file=sys.stderr,
                )
                _kill_pid(int(tracked["edge"] or 0))
                PID_PATH.unlink(missing_ok=True)
                code = litellm_proc.returncode if litellm_proc else 1
                return code if code is not None else 1
        time.sleep(0.5)



def cmd_restart(args: argparse.Namespace) -> int:
    """Stop edge + LiteLLM, then start again (same as stop && start).

    ``--delay`` waits before stopping so callers (e.g. Admin Apply) can finish
    sending an HTTP response before this process kills the edge.
    ``--detach`` is forwarded to ``start`` (Admin auto-restart always uses it).
    """
    delay = float(getattr(args, "delay", 0) or 0)
    if delay > 0:
        print(f"restart: waiting {delay:.1f}s before stop…")
        time.sleep(delay)
    print("restart: stopping…")
    cmd_stop(args)
    # Brief settle so ports / process groups fully release
    time.sleep(0.3)
    print("restart: starting…")
    return cmd_start(args)


def cmd_cache_stats(_args: argparse.Namespace | None = None) -> int:
    """Summarize cache hits / tokens_saved from ledger (+ disk entry count)."""
    ensure_deps()
    cfg = load_config()
    ledger_file = Path(cfg.ledger_path)
    if not ledger_file.is_absolute():
        ledger_file = repo_root() / ledger_file

    total = 0
    hits = 0
    tokens_saved = 0
    tokens_saved_prompt = 0
    tokens_saved_completion = 0
    by_kind: dict[str, int] = {}

    if ledger_file.exists():
        with ledger_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Only count lines that know about cache fields (post-feature)
                if "cache_hit" not in row:
                    continue
                total += 1
                if row.get("cache_hit"):
                    hits += 1
                kind = str(row.get("cache_kind") or "none")
                by_kind[kind] = by_kind.get(kind, 0) + 1
                try:
                    tokens_saved += int(row.get("tokens_saved") or 0)
                    tokens_saved_prompt += int(row.get("tokens_saved_prompt") or 0)
                    tokens_saved_completion += int(row.get("tokens_saved_completion") or 0)
                except (TypeError, ValueError):
                    pass

    from .cache import ResponseCache, cheap_pool_embedding_api_base

    disk = ResponseCache(
        cfg.cache,
        default_embedding_api_base=cheap_pool_embedding_api_base(cfg.models.cheap),
    ).stats()
    hit_rate = (hits / total) if total else 0.0
    print(f"cache.enabled:     {cfg.cache.enabled}")
    print(f"cache.mode:        {cfg.cache.mode}")
    if cfg.cache.mode == "semantic":
        print(f"embedding_model:   {cfg.cache.embedding_model}")
        print(f"similarity_thr:    {cfg.cache.similarity_threshold}")
        print(f"embed_entries:     {disk.get('entries_with_embedding', 0)}")
    print(f"cache.path:        {disk['path']}")
    print(f"disk.entries:      {disk['entries']} / {disk['max_entries']}")
    print(f"ledger:            {ledger_file}")
    print(f"requests (w/cache fields): {total}")
    print(f"hits:              {hits}")
    print(f"hit_rate:          {hit_rate:.1%}")
    print(f"tokens_saved:      {tokens_saved}")
    print(f"  prompt:          {tokens_saved_prompt}")
    print(f"  completion:      {tokens_saved_completion}")
    if by_kind:
        print("by cache_kind:", ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    return 0




def cmd_models_list(_args: argparse.Namespace | None = None) -> int:
    """Show cheap/expensive pool entries (alias, litellm_model, api_base, enabled)."""
    ensure_deps()
    from .models_yaml import default_gateway_path, list_pool_rows

    cfg = load_config()
    rows = list_pool_rows(cfg)
    if not rows:
        print("models: (none configured)")
        return 0
    print(f"models (source: {default_gateway_path()}):")
    for row in rows:
        en = "enabled" if row["enabled"] else "DISABLED"
        print(
            f"  [{row['pool']}] alias={row['alias']!r} "
            f"litellm_model={row['litellm_model']!r} "
            f"api_base={row['api_base']!r}  [{en}]"
        )
    return 0


def cmd_models_set(args: argparse.Namespace) -> int:
    """Edit primary cheap/expensive entry in gateway.yaml (model id + api_base)."""
    ensure_deps()
    from .models_yaml import (
        POOLS,
        apply_pool_set,
        default_gateway_path,
        dump_raw_yaml,
        load_raw_yaml,
    )

    pool = (args.pool or "").strip().lower()
    if pool not in POOLS:
        print(f"error: --pool must be one of {POOLS}", file=sys.stderr)
        return 1

    if args.enable and args.disable:
        print("error: use only one of --enable / --disable", file=sys.stderr)
        return 1

    has_model = args.model is not None
    has_api = getattr(args, "api_base", None) is not None or getattr(
        args, "clear_api_base", False
    )
    has_toggle = bool(args.enable or args.disable)
    has_name = bool(getattr(args, "name", None))
    if not (has_model or has_api or has_toggle or has_name):
        print(
            "error: provide --model and/or --api-base / --clear-api-base "
            "and/or --enable/--disable (and optional --name)",
            file=sys.stderr,
        )
        return 1

    path = Path(args.config) if getattr(args, "config", None) else default_gateway_path()
    raw = load_raw_yaml(path)

    api_base_arg: object = ...
    if getattr(args, "clear_api_base", False):
        api_base_arg = None
    elif getattr(args, "api_base", None) is not None:
        api_base_arg = args.api_base

    enabled = True if args.enable else (False if args.disable else None)
    try:
        entry = apply_pool_set(
            raw,
            pool,
            model=args.model,
            api_base=api_base_arg,
            enabled=enabled,
            name=getattr(args, "name", None),
        )
    except (ValueError, TypeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    kind = "dry-run" if args.dry_run else "updated"
    print(f"{kind} {path}  pool={pool}")
    print(
        f"  alias={entry.get('name')!r} "
        f"litellm_model={entry.get('litellm_model')!r} "
        f"api_base={entry.get('api_base')!r} "
        f"enabled={entry.get('enabled', True)}"
    )
    if args.dry_run:
        print("no write (--dry-run)")
        return 0

    dump_raw_yaml(path, raw)
    print(
        "note: PyYAML rewrite may drop inline comments "
        "(see config/gateway.yaml.example for the annotated template)."
    )
    print("restart to apply: python -m edge restart")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Dispatch `models` / `models list` / `models set`."""
    sub = getattr(args, "models_command", None) or "list"
    if sub == "set":
        return cmd_models_set(args)
    return cmd_models_list(args)


def _doctor_probe_ollama(cfg: GatewayConfig) -> None:
    """Soft-check configured Ollama api_base hosts (warn only; never fails doctor)."""
    from .models_yaml import collect_ollama_bases, probe_ollama_tags

    bases = collect_ollama_bases(cfg)
    if not bases:
        print("ollama probe: (no ollama api_base in cheap/expensive pools)")
        return
    print("ollama probe:")
    for row in bases:
        ok, names, err = probe_ollama_tags(row["api_base"])
        label = f"{row['api_base']}  (pool={row['pool']} alias={row['alias']})"
        if ok:
            preview = ", ".join(names[:12])
            more = f" (+{len(names) - 12} more)" if len(names) > 12 else ""
            print(f"  reachable: {label}")
            print(f"    models: {preview or '(none)'}{more}")
        else:
            print(f"  UNREACHABLE: {label}")
            print(f"    warn: {err}  (cheap/Ollama may fail until host is up)")


def cmd_doctor(_args: argparse.Namespace | None = None) -> int:
    """Check required edge deps; if config is litellm mode, also require LiteLLM.

    Also soft-probes Ollama ``api_base`` hosts (``GET /api/tags``) — warns if down.
    """
    ensure_deps()
    cfg = load_config()
    rc = 0
    if cfg.upstream_mode == "litellm":
        if not litellm_ready():
            print(format_litellm_missing_message(), file=sys.stderr)
            rc = 1
        else:
            print(preflight_ok_message(litellm_checked=True))
            cmd = resolve_litellm_cmd()
            if cmd:
                print(f"LiteLLM launch: {' '.join(cmd)}")
    else:
        # mock (or other): litellm optional
        print(preflight_ok_message(litellm_checked=False))
        if not litellm_ready():
            print(
                "note: litellm not installed (OK for mock mode). "
                "For Ollama/real providers: pip install -r requirements-litellm.txt"
            )
    _doctor_probe_ollama(cfg)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m edge",
        description="Harbor AI Gateway CLI — manage edge (+ LiteLLM) from one config",
    )
    sub = p.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="generate LiteLLM config; start edge (+ litellm)")
    start_p.add_argument(
        "--detach",
        "-d",
        action="store_true",
        help="run in background (write pidfile and exit)",
    )
    start_p.add_argument(
        "--open-browser",
        action="store_true",
        help="open http://127.0.0.1:<port>/admin after edge is up (also: admin.open_browser in YAML)",
    )
    start_p.set_defaults(func=cmd_start)

    stop_p = sub.add_parser("stop", help="stop edge and LiteLLM via pidfile/ports")
    stop_p.set_defaults(func=cmd_stop)

    restart_p = sub.add_parser(
        "restart",
        help="stop then start edge (+ LiteLLM); use --detach for background",
    )
    restart_p.add_argument(
        "--detach",
        "-d",
        action="store_true",
        help="run start in background (write pidfile and exit)",
    )
    restart_p.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="SEC",
        help="seconds to wait before stop (Admin Apply uses this so HTTP can finish)",
    )
    restart_p.add_argument(
        "--open-browser",
        action="store_true",
        help="open admin UI after start (same as start --open-browser)",
    )
    restart_p.set_defaults(func=cmd_restart)

    status_p = sub.add_parser("status", help="show pids/ports and model pools")
    status_p.set_defaults(func=cmd_status)

    check_p = sub.add_parser(
        "config-check",
        help="print resolved model aliases that will be sent to LiteLLM",
    )
    check_p.set_defaults(func=cmd_config_check)

    cache_p = sub.add_parser(
        "cache-stats",
        help="summarize cache hits / tokens_saved from ledger",
    )
    cache_p.set_defaults(func=cmd_cache_stats)

    doctor_p = sub.add_parser(
        "doctor",
        aliases=["deps-check"],
        help="verify edge deps (+ litellm when litellm mode); soft-probe Ollama api_base",
    )
    doctor_p.set_defaults(func=cmd_doctor)

    models_p = sub.add_parser(
        "models",
        help="list or set cheap/expensive model id + api_base in gateway.yaml",
    )
    models_p.set_defaults(func=cmd_models, models_command="list")
    models_sub = models_p.add_subparsers(dest="models_command")

    models_list_p = models_sub.add_parser(
        "list", help="show cheap/expensive alias, litellm_model, api_base, enabled"
    )
    models_list_p.set_defaults(func=cmd_models, models_command="list")

    models_set_p = models_sub.add_parser(
        "set",
        help="edit primary entry for a pool (writes gateway.yaml; restart to apply)",
    )
    models_set_p.add_argument(
        "--pool",
        required=True,
        choices=["cheap", "expensive"],
        help="which pool to edit",
    )
    models_set_p.add_argument(
        "--model",
        help="model id (e.g. qwen3:8b or gpt-4o-mini); prefixes ollama_chat/ or openai/ if bare",
    )
    models_set_p.add_argument(
        "--api-base",
        dest="api_base",
        help="provider base URL (e.g. http://127.0.0.1:11434 for Ollama)",
    )
    models_set_p.add_argument(
        "--clear-api-base",
        action="store_true",
        help="remove api_base (cloud providers)",
    )
    models_set_p.add_argument(
        "--name",
        help="optional alias (LiteLLM model_name), e.g. ollama-cheap",
    )
    en = models_set_p.add_mutually_exclusive_group()
    en.add_argument("--enable", action="store_true", help="mark pool primary entry enabled")
    en.add_argument("--disable", action="store_true", help="mark pool primary entry disabled")
    models_set_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resulting entry without writing gateway.yaml",
    )
    models_set_p.add_argument(
        "--config",
        help="gateway.yaml path (default: GATEWAY_CONFIG or config/gateway.yaml)",
    )
    models_set_p.set_defaults(func=cmd_models, models_command="set")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    # Defense in depth when CLI is imported without going through __main__
    if args.command in (
        "start",
        "restart",
        "status",
        "config-check",
        "cache-stats",
        "doctor",
        "deps-check",
        "models",
    ):
        ensure_deps()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

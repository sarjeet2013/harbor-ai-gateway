"""CLI: python -m edge {start|stop|status|config-check|cache-stats}"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import GatewayConfig, load_config, repo_root
from .litellm_gen import resolved_aliases, write_litellm_config

PID_PATH = repo_root() / "data" / "gateway.pid"
EDGE_LOG = repo_root() / "logs" / "edge.log"
LITELLM_LOG = repo_root() / "logs" / "litellm.log"


def _ensure_dirs() -> None:
    (repo_root() / "data").mkdir(parents=True, exist_ok=True)
    (repo_root() / "logs").mkdir(parents=True, exist_ok=True)


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


def _warn_cloud_keys(cfg: GatewayConfig) -> None:
    specs = cfg.models.all_specs()
    if not specs:
        return
    all_ollama = all(
        (s.litellm_model or "").startswith("ollama/") for s in specs if s.litellm_model
    )
    if not all_ollama:
        return
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_API_KEY"):
        if os.environ.get(key):
            print(
                f"warning: {key} is set but all models are ollama/* — "
                "Ollama does not need cloud keys; ignore or unset if unintended.",
                file=sys.stderr,
            )


def _find_litellm_cmd() -> list[str]:
    # Prefer `litellm` on PATH (venv), else python -m litellm
    from shutil import which

    litellm_bin = which("litellm")
    if litellm_bin:
        return [litellm_bin]
    return [sys.executable, "-m", "litellm"]


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
    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo_root()),
        "stdout": log_f,
        "stderr": subprocess.STDOUT,
        "start_new_session": True,  # own process group for clean kill
    }
    try:
        return subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError as e:
        raise SystemExit(
            "litellm not found. Install with: pip install 'litellm[proxy]'\n"
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


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config()
    _ensure_dirs()
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

    if args.detach:
        print("detached. stop with: python -m edge stop")
        return 0

    print("foreground — Ctrl+C to stop")

    def _shutdown(signum: int, _frame: Any) -> None:
        print(f"\nreceived signal {signum}, shutting down…")
        if edge_proc:
            _kill_pid(edge_proc.pid)
        if litellm_proc:
            _kill_pid(litellm_proc.pid)
        PID_PATH.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Wait on edge; if it dies, tear down litellm too
    while True:
        code = edge_proc.poll()
        if code is not None:
            print(f"edge exited (code={code})")
            if litellm_proc:
                _kill_pid(litellm_proc.pid)
            PID_PATH.unlink(missing_ok=True)
            return code or 0
        if litellm_proc and litellm_proc.poll() is not None:
            print(
                f"LiteLLM exited (code={litellm_proc.returncode}); stopping edge",
                file=sys.stderr,
            )
            _kill_pid(edge_proc.pid)
            PID_PATH.unlink(missing_ok=True)
            return litellm_proc.returncode or 1
        time.sleep(0.5)



def cmd_cache_stats(_args: argparse.Namespace | None = None) -> int:
    """Summarize cache hits / tokens_saved from why-log (+ disk entry count)."""
    cfg = load_config()
    why_path = Path(cfg.why_log_path)
    if not why_path.is_absolute():
        why_path = repo_root() / why_path

    total = 0
    hits = 0
    tokens_saved = 0
    tokens_saved_prompt = 0
    tokens_saved_completion = 0
    by_kind: dict[str, int] = {}

    if why_path.exists():
        with why_path.open() as f:
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

    from .cache import ResponseCache

    disk = ResponseCache(cfg.cache).stats()
    hit_rate = (hits / total) if total else 0.0
    print(f"cache.enabled:     {cfg.cache.enabled}")
    print(f"cache.mode:        {cfg.cache.mode}")
    print(f"cache.path:        {disk['path']}")
    print(f"disk.entries:      {disk['entries']} / {disk['max_entries']}")
    print(f"why-log:           {why_path}")
    print(f"requests (w/cache fields): {total}")
    print(f"hits:              {hits}")
    print(f"hit_rate:          {hit_rate:.1%}")
    print(f"tokens_saved:      {tokens_saved}")
    print(f"  prompt:          {tokens_saved_prompt}")
    print(f"  completion:      {tokens_saved_completion}")
    if by_kind:
        print("by cache_kind:", ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    return 0


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
    start_p.set_defaults(func=cmd_start)

    stop_p = sub.add_parser("stop", help="stop edge and LiteLLM via pidfile/ports")
    stop_p.set_defaults(func=cmd_stop)

    status_p = sub.add_parser("status", help="show pids/ports and model pools")
    status_p.set_defaults(func=cmd_status)

    check_p = sub.add_parser(
        "config-check",
        help="print resolved model aliases that will be sent to LiteLLM",
    )
    check_p.set_defaults(func=cmd_config_check)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

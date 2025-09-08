#!/usr/bin/env python3
"""
start_services_v2.py

Improved starter for the local AI + Supabase stack:
- Dynamic Supabase branch detection
- Safer env handling
- Readiness probe for Postgres instead of fixed sleep
- Pure Python SearXNG secret key generation
- cap_drop first-run toggle (regex; reversible)
- Unified docker compose down (all files + overrides)
"""

import os
import sys
import subprocess
import shutil
import time
import argparse
import platform
import socket
import re
from pathlib import Path
from secrets import token_hex

PROJECT_NAME = "localai"

# -------- Logging / Helpers --------

def log(msg: str):
    print(f"[start] {msg}", flush=True)

def run_command(cmd, cwd=None, allow_fail=False):
    log(f"RUN {' '.join(cmd)} (cwd={cwd or os.getcwd()})")
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
        return True
    except FileNotFoundError:
        log(f"ERROR: Command not found: {cmd[0]}")
        if not allow_fail:
            raise
    except subprocess.CalledProcessError as e:
        log(f"ERROR: Command failed ({e.returncode}): {' '.join(cmd)}")
        if not allow_fail:
            raise
    return False

def ensure_binaries():
    required = ["git", "docker"]
    missing = [b for b in required if shutil.which(b) is None]
    if missing:
        log(f"Missing required tools: {', '.join(missing)}")
        sys.exit(1)
    # Check docker compose v2
    res = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if res.returncode != 0:
        log("Docker Compose v2 plugin not available (docker compose). Install/update Docker.")
        sys.exit(1)

def resolve_supabase_branch():
    for branch in ("master", "main"):
        r = subprocess.run(
            ["git", "ls-remote", "--heads", "https://github.com/supabase/supabase.git", branch],
            capture_output=True, text=True
        )
        if branch in r.stdout:
            return branch
    log("Could not determine Supabase default branch (tried master, main).")
    sys.exit(1)

def clone_or_update_supabase(root: Path):
    repo_dir = root / "supabase"
    if not repo_dir.exists():
        log("Cloning Supabase repository (sparse checkout of docker/)...")
        run_command([
            "git", "clone", "--filter=blob:none", "--no-checkout",
            "https://github.com/supabase/supabase.git", "supabase"
        ], cwd=root)
        branch = resolve_supabase_branch()
        run_command(["git", "sparse-checkout", "init", "--cone"], cwd=repo_dir)
        run_command(["git", "sparse-checkout", "set", "docker"], cwd=repo_dir)
        run_command(["git", "checkout", branch], cwd=repo_dir)
    else:
        log("Updating existing Supabase repository...")
        run_command(["git", "fetch", "--prune"], cwd=repo_dir, allow_fail=True)
        run_command(["git", "pull", "--ff-only"], cwd=repo_dir)

def prepare_supabase_env(root: Path):
    src = root / ".env"
    dest = root / "supabase" / "docker" / ".env"
    if not src.exists():
        log(f"Root .env not found at {src}; skipping copy.")
        return
    if not dest.exists():
        log("Copying root .env to supabase/docker/.env")
        shutil.copyfile(src, dest)
        return
    log("Merging any new variables from root .env into supabase/docker/.env")
    existing = {}
    with dest.open() as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                existing[k] = v
    additions = []
    with src.open() as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                if k not in existing:
                    additions.append(line)
    if additions:
        with dest.open("a") as f:
            f.write("\n# Added from root .env\n")
            f.writelines(additions)

def generate_searxng_secret_key(root: Path):
    settings_base = root / "searxng" / "settings-base.yml"
    settings = root / "searxng" / "settings.yml"
    if not settings_base.exists():
        log(f"SearXNG base settings missing ({settings_base}); skipping secret generation.")
        return
    if not settings.exists():
        log("Creating searxng/settings.yml from base.")
        shutil.copyfile(settings_base, settings)
    text = settings.read_text(encoding="utf-8")
    if "ultrasecretkey" not in text:
        log("SearXNG secret already replaced; skipping.")
        return
    secret = token_hex(32)
    settings.write_text(text.replace("ultrasecretkey", secret), encoding="utf-8")
    log("SearXNG secret key inserted.")

def adjust_cap_drop_first_run(root: Path):
    compose_file = root / "docker-compose.yml"
    if not compose_file.exists():
        log("docker-compose.yml not found; skipping cap_drop adjustments.")
        return
    content = compose_file.read_text(encoding="utf-8")

    ps = subprocess.run(
        ["docker", "ps", "--filter", "name=searxng", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    containers = [c.strip() for c in ps.stdout.splitlines() if c.strip()]
    first_run = True
    if containers:
        name = containers[0]
        probe = subprocess.run(
            ["docker", "exec", name, "sh", "-c", "[ -f /etc/searxng/uwsgi.ini ] && echo found || echo missing"],
            capture_output=True, text=True
        )
        first_run = "missing" in probe.stdout

    commented_pattern = re.compile(r"#\s*cap_drop:\s*- ALL")
    active_pattern = re.compile(r"(^\s*cap_drop:\s*- ALL)", re.MULTILINE)
    modified = False

    if first_run and active_pattern.search(content):
        log("First SearXNG run: commenting out cap_drop (temporary).")
        content = active_pattern.sub("# cap_drop: - ALL  # temporarily disabled first run", content)
        modified = True
    elif not first_run and commented_pattern.search(content):
        log("Re-enabling cap_drop for SearXNG.")
        content = commented_pattern.sub("cap_drop: - ALL", content)
        modified = True

    if modified:
        compose_file.write_text(content, encoding="utf-8")

def compose_down(root: Path, profile: str, environment: str):
    log("Stopping existing project containers.")
    cmd = ["docker", "compose", "-p", PROJECT_NAME]
    if profile and profile != "none":
        cmd += ["--profile", profile]

    # Include all known compose files so every service is targeted.
    cmd += ["-f", "supabase/docker/docker-compose.yml", "-f", "docker-compose.yml"]

    if environment == "private":
        cmd += ["-f", "docker-compose.override.private.yml"]
    elif environment == "public":
        cmd += ["-f", "docker-compose.override.public.yml", "-f", "docker-compose.override.public.supabase.yml"]

    cmd += ["down", "--remove-orphans"]
    run_command(cmd, cwd=root, allow_fail=True)

def compose_up_supabase(root: Path, environment: str):
    cmd = ["docker", "compose", "-p", PROJECT_NAME, "-f", "supabase/docker/docker-compose.yml"]
    if environment == "public":
        cmd += ["-f", "docker-compose.override.public.supabase.yml"]
    cmd += ["up", "-d"]
    run_command(cmd, cwd=root)

def compose_up_local_ai(root: Path, profile: str, environment: str):
    cmd = ["docker", "compose", "-p", PROJECT_NAME, "-f", "docker-compose.yml"]
    if environment == "private":
        cmd += ["-f", "docker-compose.override.private.yml"]
    elif environment == "public":
        cmd += ["-f", "docker-compose.override.public.yml"]
    if profile and profile != "none":
        cmd += ["--profile", profile]
    cmd += ["up", "-d"]
    run_command(cmd, cwd=root)

def wait_for_postgres(host="127.0.0.1", port=54322, timeout=120):
    log(f"Waiting for Postgres at {host}:{port} (timeout {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=2):
                log("Postgres reachable.")
                return True
        except OSError:
            time.sleep(2)
    log("Postgres not reachable within timeout; continuing anyway.")
    return False

def main():
    parser = argparse.ArgumentParser(description="Start local AI + Supabase stack.")
    parser.add_argument("--profile", choices=["cpu", "gpu-nvidia", "gpu-amd", "none"], default="cpu")
    parser.add_argument("--environment", choices=["private", "public"], default="private")
    parser.add_argument("--skip-clone", action="store_true", help="Skip Supabase clone/update step.")
    parser.add_argument("--no-cap-adjust", action="store_true", help="Skip automatic cap_drop adjustment.")
    parser.add_argument("--wait-seconds", type=int, default=0, help="Extra sleep after Supabase up (fallback).")
    parser.add_argument("--pg-port", type=int, default=54322, help="Supabase Postgres mapped port to probe.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    ensure_binaries()

    if not args.skip_clone:
        clone_or_update_supabase(root)
    prepare_supabase_env(root)
    generate_searxng_secret_key(root)
    if not args.no_cap_adjust:
        adjust_cap_drop_first_run(root)

    compose_down(root, args.profile, args.environment)
    compose_up_supabase(root, args.environment)

    # Readiness probe (can be overridden if compose file uses another mapping)
    ready = wait_for_postgres(port=args.pg_port)
    if not ready and args.wait_seconds > 0:
        log(f"Fallback sleep {args.wait_seconds}s.")
        time.sleep(args.wait_seconds)

    compose_up_local_ai(root, args.profile, args.environment)
    log("Startup sequence complete.")

if __name__ == "__main__":
    main()
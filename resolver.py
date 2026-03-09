import os
import json
import tomllib
import requests
import subprocess
from pathlib import Path

CONFIG_FILE = "config.toml"
VERSIONS_FILE = "versions.json"

PEACHMEOW_GITHUB_PAT = os.environ.get("PEACHMEOW_GITHUB_PAT")

HEADERS = {}
if PEACHMEOW_GITHUB_PAT:
    HEADERS["Authorization"] = f"token {PEACHMEOW_GITHUB_PAT}"

def die(m):
    print(m, flush=True)
    raise SystemExit(1)

def load_config():
    if not Path(CONFIG_FILE).exists():
        die("config.toml missing")
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)

def load_versions():
    if not Path(VERSIONS_FILE).exists():
        return {}
    return json.loads(Path(VERSIONS_FILE).read_text())

def resolve(repo, mode):
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=HEADERS,
        timeout=60
    )
    if r.status_code != 200:
        die(f"Failed to fetch {repo}")

    rel = r.json()

    if not rel:
        return None

    if mode == "latest":
        for x in rel:
            if not x["prerelease"]:
                return x["tag_name"].lstrip("v")

    if mode == "dev":
        for x in rel:
            if x["prerelease"]:
                return x["tag_name"].lstrip("v")
        return None

    if mode == "all":
        return rel[0]["tag_name"].lstrip("v")

    return mode

def resolve_channels(repo):
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=HEADERS,
        timeout=60
    )
    if r.status_code != 200:
        die(f"Failed to fetch {repo}")

    rel = r.json()

    latest = None
    dev = None

    for x in rel:
        if not latest and not x["prerelease"]:
            latest = x["tag_name"].lstrip("v")
        if not dev and x["prerelease"]:
            dev = x["tag_name"].lstrip("v")
        if latest and dev:
            break

    return latest, dev

def trigger(src):
    print(f"[+] Trigger build: {src}")
    subprocess.run(
        ["gh", "workflow", "run", "build.yml", "-f", f"source={src}"],
        check=True
    )

def main():
    print("[+] Resolver started")

    cfg = load_config()
    old = load_versions()

    global_patches = cfg.get("patches-source") or "MorpheApp/morphe-patches"
    global_mode = cfg.get("patches-version") or "latest"

    apps = {k: v for k, v in cfg.items() if isinstance(v, dict)}

    sources = {}

    for app in apps.values():
        if app.get("enabled", True) is False:
            continue

        src = app.get("patches-source") or global_patches
        mode = app.get("patches-version") or global_mode

        sources[src] = mode

    active = set(sources.keys())

    dirty = False
    removed = []

    for k in list(old.keys()):
        if k not in active:
            print("[-] Removing stale source from versions.json:", k)
            old.pop(k)
            removed.append(k)
            dirty = True

    if dirty:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))

        subprocess.run(["git","config","user.name","github-actions[bot]"], check=True)
        subprocess.run(["git","config","user.email","41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git","add",VERSIONS_FILE], check=True)

        if len(removed) == 1:
            msg = f"delete: stale patch source → {removed[0]}"
        else:
            msg = "delete: stale patch sources → " + ", ".join(removed)

        subprocess.run(["git","commit","-m", msg], check=False)
        subprocess.run(["git","push"], check=True)

    changed = []

    for src, mode in sources.items():

        stored = old.get(src, {})

        if mode == "latest":
            if "dev" in stored:
                stored.pop("dev")
                dirty = True
                removed.append(src)
        elif mode == "dev":
            if "latest" in stored:
                stored.pop("latest")
                dirty = True
                removed.append(src)
        elif mode != "all":
            if "latest" in stored and "dev" in stored:
                stored.pop("dev")
                dirty = True
                removed.append(src)

        if mode != "all":

            latest = resolve(src, mode)

            if mode == "dev":
                prev_version = stored.get("dev", {}).get("patch")
            else:
                prev_version = stored.get("latest", {}).get("patch")

            print(src)
            print("  latest :", latest)
            print("  stored :", prev_version)

            if latest and latest != prev_version:
                changed.append(src)

            continue

        latest_stable, latest_dev = resolve_channels(src)

        stored_latest = stored.get("latest", {}).get("patch")
        stored_dev = stored.get("dev", {}).get("patch")

        print(src)
        print("  upstream latest :", latest_stable)
        print("  upstream dev    :", latest_dev)
        print("  stored latest   :", stored_latest)
        print("  stored dev      :", stored_dev)

        stable_changed = latest_stable and latest_stable != stored_latest
        dev_changed = latest_dev and latest_dev != stored_dev

        if stable_changed:
            changed.append(("stable", src))
            continue

        if dev_changed:
            changed.append(("dev", src))

    if not changed:
        print("[✓] No patch updates")
        return

    for item in changed:

        if isinstance(item, tuple):
            channel, src = item

            if channel == "stable":

                cfg_text = Path(CONFIG_FILE).read_text()
                lines = []

                current_block = None
                current_src = global_patches

                for line in cfg_text.splitlines():

                    stripped = line.strip()

                    if stripped.startswith("[") and stripped.endswith("]"):
                        current_block = stripped
                        current_src = global_patches
                        lines.append(line)
                        continue

                    if "=" in line and current_block:
                        key, val = line.split("=",1)
                        key = key.strip()

                        if key == "patches-source":
                            current_src = val.strip().strip('"')

                        if current_src == src and key in {"patches-version", "cli-version"}:
                            continue

                    lines.append(line)

                Path(CONFIG_FILE).write_text("\n".join(lines))

                trigger(src)

            else:
                trigger(src)

        else:
            trigger(item)

    if dirty and removed:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))

        subprocess.run(["git","config","user.name","github-actions[bot]"], check=True)
        subprocess.run(["git","config","user.email","41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git","add",VERSIONS_FILE], check=True)

        if len(removed) == 1:
            msg = f"delete: unused version channels → {removed[0]}"
        else:
            msg = "delete: unused version channels → " + ", ".join(removed)

        subprocess.run(["git","commit","-m", msg], check=False)
        subprocess.run(["git","push"], check=True)

    print("[✓] Resolver done")

if __name__ == "__main__":
    main()

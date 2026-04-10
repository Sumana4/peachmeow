import os
import json
import tomllib
import requests
import subprocess
from pathlib import Path
from packaging.version import Version
from utils import *

CONFIG_FILE = "config.toml"
VERSIONS_FILE = "versions.json"

PEACHMEOW_GITHUB_PAT = os.environ.get("PEACHMEOW_GITHUB_PAT")

HEADERS = {}
if PEACHMEOW_GITHUB_PAT:
    HEADERS["Authorization"] = f"token {PEACHMEOW_GITHUB_PAT}"


def load_config():
    if not Path(CONFIG_FILE).exists():
        die("config.toml missing")
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def load_versions():
    if not Path(VERSIONS_FILE).exists():
        return {}

    txt = Path(VERSIONS_FILE).read_text().strip()
    if not txt:
        return {}

    return json.loads(txt)


def resolve(repo, mode):
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases", headers=HEADERS, timeout=60
    )
    if r.status_code != 200:
        die(f"Failed to fetch {repo}")

    rel = r.json()

    if not rel:
        return None, False

    if mode == "latest":
        for x in rel:
            if not x["prerelease"]:
                return x["tag_name"], False
        return None, False

    if mode == "dev":
        for x in rel:
            if x["prerelease"]:
                return x["tag_name"], True
        return None, True

    if mode == "all":
        return rel[0]["tag_name"], rel[0]["prerelease"]

    tag = mode

    for x in rel:
        if x["tag_name"] == tag:
            return tag, x["prerelease"]

    return None, False


def resolve_channels(repo):
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases", headers=HEADERS, timeout=60
    )
    if r.status_code != 200:
        die(f"Failed to fetch {repo}")

    rel = r.json()

    latest = None
    dev = None

    for x in rel:
        tag = x["tag_name"]

        if x["prerelease"]:
            if dev is None:
                dev = tag
        else:
            if latest is None:
                latest = tag

    return latest, dev


def trigger(src, mode=None):
    log_sub("Trigger Build")
    log_source(src)

    display_mode = mode if mode else "None"
    log_kv("Mode", display_mode)

    cmd = ["gh", "workflow", "run", "build.yml", "-f", f"source={src}"]
    if mode:
        cmd += ["-f", f"mode={mode}"]

    subprocess.run(cmd, check=True)


def main():
    log_plain_section("Resolver Start")

    cfg = load_config()

    subprocess.run(["git", "fetch", "origin", "state"], check=False)

    remote_check = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", "state"],
        capture_output=True,
        text=True,
    )

    state_exists = remote_check.stdout.strip() != ""

    if not state_exists:
        old = {}
        versions_file_existed = False
    else:
        subprocess.run(["git", "checkout", "-B", "state", "origin/state"], check=True)

        versions_file_existed = Path(VERSIONS_FILE).exists()
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

    source_dirty = False
    channel_dirty = False
    removed_sources = []
    removed_channels = []

    for k in list(old.keys()):
        if k not in active:
            log_sub("Cleanup")
            log_info(f"Removing stale patch source: {k}")
            old.pop(k)
            removed_sources.append(k)
            source_dirty = True

    if source_dirty and state_exists and versions_file_existed:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))

        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"], check=True
        )
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)

        if len(removed_sources) == 1:
            msg = f"delete: stale patch source → {removed_sources[0]}"
        else:
            msg = "delete: stale patch sources → " + ", ".join(removed_sources)

        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=True)

    changed = []
    log_sub("Check")

    for src, mode in sources.items():

        stored = old.get(src, {})

        if mode == "latest":
            if "dev" in stored:
                stored.pop("dev")
                channel_dirty = True
                removed_channels.append(src)
        elif mode == "dev":
            if "latest" in stored:
                stored.pop("latest")
                channel_dirty = True
                removed_channels.append(src)
        elif mode != "all":
            if "latest" in stored and "dev" in stored:
                stored.pop("dev")
                channel_dirty = True
                removed_channels.append(src)

        if mode != "all":

            latest, is_pre = resolve(src, mode)

            if is_pre:
                prev_version = stored.get("dev", {}).get("patch")
            else:
                prev_version = stored.get("latest", {}).get("patch")

            log_source(src)

            if mode in ["latest", "dev"]:
                status = (
                    "SKIPPED"
                    if latest is None
                    else (
                        "UPDATE AVAILABLE" if latest != prev_version else "UP TO DATE"
                    )
                )
            else:
                status = (
                    "NOT FOUND"
                    if latest is None
                    else (
                        "UPDATE AVAILABLE" if latest != prev_version else "UP TO DATE"
                    )
                )

            if mode in ["latest", "dev"]:
                log_version_status(
                    mode,
                    [
                        ("Upstream", latest),
                        ("Stored", prev_version),
                    ],
                    status,
                )
            else:
                log_version_status(
                    mode,
                    [
                        ("Requested", mode),
                        ("Stored", prev_version),
                    ],
                    status,
                )

            if latest and latest != prev_version:
                changed.append(src)

            continue

        latest_stable, latest_dev = resolve_channels(src)

        stored_latest = stored.get("latest", {}).get("patch")
        stored_dev = stored.get("dev", {}).get("patch")

        log_source(src)

        if latest_stable is None and latest_dev is None:
            status = "SKIPPED"
        else:
            stable_changed = latest_stable and (
                stored_latest is None or Version(latest_stable) > Version(stored_latest)
            )

            dev_changed = latest_dev and latest_dev != stored_dev

            if stable_changed:
                status = "UPDATE AVAILABLE"
            elif dev_changed:
                dev_base = latest_dev.split("-dev", 1)[0]
                if stored_latest and Version(dev_base) <= Version(stored_latest):
                    status = "UP TO DATE"
                else:
                    status = "UPDATE AVAILABLE"
            else:
                status = "UP TO DATE"

        log_version_status(
            "all",
            [
                ("Stable Upstream", latest_stable),
                ("Stable Stored", stored_latest),
                ("Dev Upstream", latest_dev),
                ("Dev Stored", stored_dev),
            ],
            status,
        )

        stable_changed = latest_stable and (
            stored_latest is None or Version(latest_stable) > Version(stored_latest)
        )

        if stable_changed:
            changed.append(("stable", src))
            continue

        dev_changed = latest_dev and latest_dev != stored_dev

        if dev_changed:
            dev_base = latest_dev.split("-dev", 1)[0]
            if stored_latest and Version(dev_base) <= Version(stored_latest):
                continue
            changed.append(("dev", src))

    if not changed:
        log_space()
        log_info("No patch updates")
        log_space()
        return

    log_space()
    count = len(changed)
    log_info(f"Changes detected: {count} patch source" + ("s" if count != 1 else ""))

    for item in changed:

        if isinstance(item, tuple):
            channel, src = item

            if channel == "stable":
                trigger(src, "latest")
            else:
                trigger(src)

        else:
            trigger(item)

    if channel_dirty and removed_channels and state_exists and versions_file_existed:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))

        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"], check=True
        )
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)

        if len(removed_channels) == 1:
            msg = f"delete: unused version channel → {removed_channels[0]}"
        else:
            msg = "delete: unused version channels → " + ", ".join(removed_channels)

        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=True)

    log_plain_section("Resolver Complete")
    log_done("Resolver finished successfully")
    log_space()


if __name__ == "__main__":
    main()

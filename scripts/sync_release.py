#!/usr/bin/env python3
"""Sync successful BakaSU CI builds into this repository's GitHub Releases.

The script discovers completed, successful "Build Manager" runs on the upstream
default branch, downloads the artifacts they produced, republishes them under a
per-run tag, and records full provenance (commit SHA, run id, run URL, build
time).

Design decisions that are specific to BakaSU (verified against live runs):

* ``Manager-release`` and ``Spoofed-Manager-release`` contain APKs with
  *identical* file names, so the spoofed variant is deliberately excluded to
  avoid clobbering the normal build.
* ``aarch64-<kmi>-lkm`` and ``x86_64-<kmi>-lkm`` both contain
  ``<kmi>_kernelsu.ko``. Aggregating them into a single archive would overwrite
  one architecture with the other, so modules are grouped per architecture.
* ``Manager-mappings`` (ProGuard mappings) and ``telegram-bot-api-binary`` are
  CI-only tooling and are never published.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"

DEFAULT_TARGET_REPO = "Baka-SU/BakaSU"
DEFAULT_WORKFLOW = "build-manager.yml"
DEFAULT_BRANCH = "main"
DEFAULT_LIMIT = 50

# Artifacts that are never published (CI tooling / debug info / duplicate build).
EXCLUDE_EXACT = {"telegram-bot-api-binary"}
EXCLUDE_PREFIX = ("Spoofed-",)
EXCLUDE_SUFFIX = ("-mappings",)

APK_ARTIFACTS = ("Manager-release", "Manager-debug")
# A run must contain this artifact to be considered a complete, publishable build.
REQUIRED_ARTIFACTS = ("Manager-release",)

LKM_RE = re.compile(r"^(aarch64|x86_64)-(.+)-lkm$")
KSUD_RE = re.compile(r"^ksud-(.+)$")
KSUINIT_RE = re.compile(r"^ksuinit-(.+)$")

# An APK asset must be a flat, safe file name (no path traversal / odd chars).
APK_SAFE_RE = re.compile(r"^[A-Za-z0-9._+\-]+\.apk$")
# e.g. BakaSU_v4.2.0-rc3_35231-universal-release.apk -> v4.2.0-rc3
VERSION_RE = re.compile(r"^[^_]+_(?P<version>.+?)_\d+-[A-Za-z0-9_]+-release\.apk$")
PRERELEASE_RE = re.compile(r"-(rc|alpha|beta|pre|dev)\d*$", re.IGNORECASE)

TAG_PREFIX = "BakaSU_"


class ApiError(RuntimeError):
    pass


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect leaves the API host.

    Artifact downloads return a 302 to a pre-signed Azure blob URL. That URL
    already carries its own credentials, and sending our token along makes the
    storage backend reject the request with HTTP 401.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and urllib.parse.urlsplit(newurl).hostname != \
                urllib.parse.urlsplit(req.full_url).hostname:
            new_req.headers.pop("Authorization", None)
            new_req.unredirected_hdrs.pop("Authorization", None)
        return new_req


_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


def log(msg: str) -> None:
    print(msg, flush=True)


def _request(url: str, token: str, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None, retries: int = 3):
    req_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "BakaSU_CI-sync",
    }
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            return _OPENER.open(req, timeout=120)
        except urllib.error.HTTPError as exc:
            # Primary and secondary rate limits surface as 403/429.
            if exc.code in (403, 429) and attempt < retries:
                wait = _rate_limit_wait(exc)
                log(f"  ! rate limited (HTTP {exc.code}); sleeping {wait}s")
                time.sleep(wait)
                attempt += 1
                continue
            raise


def _rate_limit_wait(exc: urllib.error.HTTPError) -> int:
    retry_after = exc.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(int(retry_after), 120)
    reset = exc.headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        return max(1, min(int(reset) - int(time.time()) + 2, 120))
    return 30


def api_get_json(url: str, token: str) -> tuple[object, str | None]:
    with _request(url, token) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        link = resp.headers.get("Link")
    return payload, link


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.strip()
        if 'rel="next"' in seg:
            return seg[seg.index("<") + 1:seg.index(">")]
    return None


def api_get_all(url: str, token: str) -> list:
    items: list = []
    while url:
        payload, link = api_get_json(url, token)
        if isinstance(payload, dict):
            items.extend(payload.get("artifacts") or payload.get("workflow_runs") or [])
        else:
            items.extend(payload)
        url = next_link(link)
    return items


def api_post_json(url: str, token: str, body: dict) -> object:
    data = json.dumps(body).encode("utf-8")
    with _request(url, token, method="POST", data=data,
                  headers={"Content-Type": "application/json"}) as resp:
        return json.loads(resp.read().decode("utf-8"))


def release_exists(repo: str, tag: str, token: str) -> bool:
    try:
        with _request(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}", token):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def tag_exists(repo: str, tag: str, token: str) -> bool:
    try:
        with _request(f"{API}/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}", token):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def download_artifact(repo: str, artifact: dict, token: str, dest_dir: str) -> str | None:
    art_id = artifact["id"]
    name = artifact["name"]
    if artifact.get("expired"):
        log(f"  - {name}: artifact expired, skipping")
        return None
    url = f"{API}/repos/{repo}/actions/artifacts/{art_id}/zip"
    path = os.path.join(dest_dir, f"{art_id}.zip")
    try:
        with _request(url, token) as resp, open(path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except urllib.error.HTTPError as exc:
        log(f"  - {name}: download failed (HTTP {exc.code}), skipping")
        return None
    except OSError as exc:
        log(f"  - {name}: download failed ({exc}), skipping")
        return None

    if os.path.getsize(path) == 0:
        log(f"  - {name}: empty artifact, skipping")
        os.remove(path)
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            if not zf.namelist():
                log(f"  - {name}: artifact contains no files, skipping")
                os.remove(path)
                return None
    except zipfile.BadZipFile:
        log(f"  - {name}: not a valid zip, skipping")
        os.remove(path)
        return None
    return path


def classify(name: str) -> str:
    if name in EXCLUDE_EXACT or name.startswith(EXCLUDE_PREFIX) or name.endswith(EXCLUDE_SUFFIX):
        return "exclude"
    if name in APK_ARTIFACTS:
        return "apk"
    if LKM_RE.match(name):
        return "lkm"
    if KSUD_RE.match(name) or KSUINIT_RE.match(name):
        return "tool"
    return "exclude"


def is_valid_apk(member: str) -> bool:
    base = os.path.basename(member)
    return base == member and bool(APK_SAFE_RE.match(base))


def version_from_apk(apk_names: list[str]) -> str | None:
    # Prefer the universal APK; fall back to any release APK.
    for candidate in sorted(apk_names, key=lambda n: ("universal" not in n, n)):
        match = VERSION_RE.match(candidate)
        if match:
            return match.group("version")
    return None


def prepare_run(repo: str, run: dict, token: str, workdir: str) -> dict | None:
    run_id = run["id"]
    artifacts = api_get_all(f"{API}/repos/{repo}/actions/runs/{run_id}/artifacts", token)
    names = {a["name"] for a in artifacts}

    missing = [req for req in REQUIRED_ARTIFACTS if req not in names]
    if missing:
        log(f"  - required artifact(s) missing: {', '.join(missing)}; skipping run")
        return None

    assets: list[tuple[str, str]] = []       # (local path, published file name)
    apk_names: list[str] = []
    lkm_by_arch: dict[str, dict[str, str]] = {}

    for artifact in sorted(artifacts, key=lambda a: a["name"]):
        name = artifact["name"]
        kind = classify(name)
        if kind == "exclude":
            continue
        path = download_artifact(repo, artifact, token, workdir)
        if path is None:
            continue

        if kind == "apk":
            with zipfile.ZipFile(path) as zf:
                for member in zf.namelist():
                    if not member.endswith(".apk"):
                        continue
                    base = os.path.basename(member)
                    if not is_valid_apk(member):
                        log(f"  - {name}: ignoring invalid APK name '{member}'")
                        continue
                    if base in apk_names:
                        log(f"  - {name}: duplicate APK '{base}', skipping")
                        continue
                    out = os.path.join(workdir, base)
                    with zf.open(member) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    apk_names.append(base)
                    assets.append((out, base))
        elif kind == "lkm":
            arch = LKM_RE.match(name).group(1)
            with zipfile.ZipFile(path) as zf:
                for member in zf.namelist():
                    if not member.endswith(".ko"):
                        continue
                    base = os.path.basename(member)
                    bucket = lkm_by_arch.setdefault(arch, {})
                    if base in bucket:
                        log(f"  - {name}: duplicate module '{base}', skipping")
                        continue
                    out = os.path.join(workdir, f"{arch}_{base}")
                    with zf.open(member) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    bucket[base] = out
        else:  # ksud / ksuinit: republish the original artifact archive verbatim
            assets.append((path, f"{name}.zip"))

    if not apk_names:
        log("  - no valid APKs found; skipping run")
        return None

    for arch, modules in sorted(lkm_by_arch.items()):
        archive = os.path.join(workdir, f"lkm-{arch}-all.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for base, src in sorted(modules.items()):
                zf.write(src, arcname=base)
        assets.append((archive, f"lkm-{arch}-all.zip"))

    version = version_from_apk(apk_names) or f"run-{run_id}"
    prerelease = bool(PRERELEASE_RE.search(version))

    return {
        "run_id": run_id,
        "tag": f"{TAG_PREFIX}{run_id}",
        "version": version,
        "prerelease": prerelease,
        "assets": assets,
        "commit": run.get("head_sha", ""),
        "run_url": run.get("html_url", ""),
        "branch": run.get("head_branch", ""),
        "build_time": run.get("run_started_at") or run.get("created_at") or "",
    }


def build_body(repo: str, info: dict) -> str:
    lines = [
        f"Automatically synced from [`{repo}`](https://github.com/{repo}).",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Upstream commit | `{info['commit']}` |",
        f"| Actions run ID | `{info['run_id']}` |",
        f"| Run link | {info['run_url']} |",
        f"| Branch | `{info['branch']}` |",
        f"| Build time (UTC) | {info['build_time']} |",
        "",
        "**Assets**",
        "",
    ]
    for _, published in sorted(info["assets"], key=lambda a: a[1]):
        lines.append(f"- `{published}`")
    lines += [
        "",
        "Generated by BakaSU_CI. Upstream artifacts are published as-is; kernel "
        "modules are grouped per architecture to avoid mixing incompatible builds.",
    ]
    return "\n".join(lines)


def create_release(repo: str, info: dict, token: str) -> None:
    body = {
        "tag_name": info["tag"],
        "name": f"BakaSU {info['version']}",
        "body": build_body(repo, info),
        "draft": False,
        "prerelease": info["prerelease"],
    }
    try:
        release = api_post_json(f"{API}/repos/{repo}/releases", token, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 422 and "already_exists" in detail:
            log(f"  - tag {info['tag']} already exists upstream; skipping")
            return
        raise ApiError(f"failed to create release {info['tag']}: HTTP {exc.code} {detail}")

    release_id = release["id"]
    for path, name in sorted(info["assets"], key=lambda a: a[1]):
        upload_asset(repo, release_id, path, name, token)
    log(f"  + published {info['tag']} ({len(info['assets'])} assets)")


def upload_asset(repo: str, release_id: int, path: str, name: str, token: str) -> None:
    query = urllib.parse.urlencode({"name": name})
    url = f"{UPLOADS}/repos/{repo}/releases/{release_id}/assets?{query}"
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        _request(url, token, method="POST", data=data,
                 headers={"Content-Type": "application/octet-stream"})
        log(f"      uploaded {name}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        log(f"      failed to upload {name}: HTTP {exc.code} {detail}")


def _is_publishable(run: dict, branch: str) -> bool:
    return (
        run.get("event") != "pull_request"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_branch") == branch
    )


def fetch_runs(repo: str, workflow: str, branch: str, limit: int, token: str) -> list[dict]:
    """Return up to ``limit`` publishable runs, oldest first.

    Runs are listed newest first. Pages are followed only until enough
    publishable runs have been collected, so a noisy workflow (many PR runs)
    does not force unbounded pagination.
    """
    url = (f"{API}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
           f"?branch={urllib.parse.quote(branch)}&status=success&per_page=100")
    selected: list[dict] = []
    max_pages = 10  # hard stop against runaway pagination
    pages = 0
    while url and len(selected) < limit and pages < max_pages:
        payload, link = api_get_json(url, token)
        for run in payload.get("workflow_runs", []):
            if _is_publishable(run, branch):
                selected.append(run)
                if len(selected) >= limit:
                    break
        url = next_link(link)
        pages += 1
    return list(reversed(selected))  # oldest first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="target repository (owner/name) that publishes the releases")
    parser.add_argument("--target-repo", default=DEFAULT_TARGET_REPO,
                        help="upstream repository to sync from")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--limit", type=int,
                        default=int(os.environ.get("N_LATEST_RUNS", DEFAULT_LIMIT)))
    parser.add_argument("--dry-run", action="store_true",
                        help="prepare assets but do not create releases")
    args = parser.parse_args()

    if not args.repo and not args.dry_run:
        parser.error("--repo (or GITHUB_REPOSITORY) is required unless --dry-run is used")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        log("ERROR: GH_TOKEN / GITHUB_TOKEN is not set")
        return 1

    log(f"Fetching up to {args.limit} successful '{args.workflow}' runs "
        f"on {args.target_repo}@{args.branch}...")
    runs = fetch_runs(args.target_repo, args.workflow, args.branch, args.limit, token)
    log(f"Found {len(runs)} candidate run(s).")

    published = 0
    for run in runs:
        tag = f"{TAG_PREFIX}{run['id']}"
        log(f"--- {tag} (commit {run.get('head_sha', '')[:8]}) ---")
        if not args.dry_run and (release_exists(args.repo, tag, token)
                                 or tag_exists(args.repo, tag, token)):
            log("  - release/tag already exists; skipping")
            continue

        workdir = tempfile.mkdtemp(prefix=f"bakasu-{run['id']}-")
        try:
            info = prepare_run(args.target_repo, run, token, workdir)
            if info is None:
                continue
            log(f"  - version {info['version']} ({len(info['assets'])} assets, "
                f"prerelease={info['prerelease']})")
            for _, name in sorted(info["assets"], key=lambda a: a[1]):
                log(f"      {name}")
            if args.dry_run:
                log("  - dry run: not publishing")
            else:
                create_release(args.repo, info, token)
                published += 1
        except ApiError as exc:
            log(f"  ! {exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    log(f"Done. {published} release(s) published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

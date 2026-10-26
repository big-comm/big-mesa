#!/usr/bin/env python3
"""Keep mesa-big and lib32-mesa-big on the latest Mesa and LLVM releases.

Two jobs, as the big-kernel watchers do:

follow    Runs in the build image. Moves both PKGBUILDs, together, to the
          newest stable Mesa release at archive.mesa3d.org (the .0 of a new
          series included) and the newest LLVM release of the same major;
          checks both tarballs' signatures with the keys in mesa-big/keys/pgp,
          regenerates the NVK crate list from the new Mesa and the checksums
          (makepkg -g). Without a new release, a new spirv-tools in Manjaro
          stable (its library has no versioned soname) bumps pkgrel instead.
          A new LLVM major is left to a person: Mesa may not support it yet.

dispatch  Sends each package to be built for testing while its PKGBUILD is
          ahead of what community-testing has, up to three attempts per
          version, as the big-kernel module watcher does.

A version that does not build, or fails check(), is never published: the
build in testing is the gate, and the third failed attempt is reported.
"""

import argparse
import calendar
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

REPOSITORY_URL = "https://github.com/big-comm/big-mesa"
USER_AGENT = f"big-mesa-watch/1 (+{REPOSITORY_URL})"
WORKFLOW_REPOSITORY = "big-comm/build-package"

# Package directory -> the Manjaro package whose library it links unversioned.
PACKAGES = {
    "mesa-big": "spirv-tools",
    "lib32-mesa-big": "lib32-spirv-tools",
}
KEYS = "mesa-big/keys/pgp"

MESA_ARCHIVE = "https://archive.mesa3d.org/"
MESA_TARBALL = "https://archive.mesa3d.org/mesa-{version}.tar.xz"
LLVM_RELEASES = "https://api.github.com/repos/llvm/llvm-project/releases?per_page=100"
LLVM_TARBALL = (
    "https://github.com/llvm/llvm-project/releases/download/"
    "llvmorg-{version}/llvm-project-{version}.src.tar.xz"
)
# build-package builds every package against Manjaro stable.
MANJARO_DBS = {
    "spirv-tools": "https://mirrors.manjaro.org/repo/stable/extra/x86_64/extra.db",
    "lib32-spirv-tools": "https://mirrors.manjaro.org/repo/stable/multilib/x86_64/multilib.db",
}
COMMUNITY_DB = "https://repo.communitybig.org/community-{branch}/x86_64/community-{branch}.db"
# What a testing user sees: stable, with testing's copy taking precedence.
TESTING_VIEW = [COMMUNITY_DB.format(branch="stable"), COMMUNITY_DB.format(branch="testing")]


class PkgbuildError(Exception):
    """A PKGBUILD without a value the watcher needs, or out of step."""


def log(message):
    print(message, flush=True)


def fetch(url):
    # repo.communitybig.org refuses urllib's default User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


# --- PKGBUILDs ---------------------------------------------------------------

def value(text, key, where="PKGBUILD"):
    match = re.search(rf"^{key}=(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise PkgbuildError(f"{where}: no literal {key}=")
    return match.group(1).strip("'\"")


def set_value(text, key, new):
    updated, count = re.subn(rf"^{key}=\S+\s*$", f"{key}={new}", text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise PkgbuildError(f"no literal {key}= to set")
    return updated


def replace_array(text, name, block):
    """Replace the bash array `name=(...)`, which ends at the first ')'."""
    start = re.search(rf"^{re.escape(name)}=\(", text, re.MULTILINE)
    if not start:
        raise PkgbuildError(f"no {name}=( in the PKGBUILD")
    end = text.index(")", start.end())
    return text[:start.start()] + block + text[end + 1:]


def version_key(version):
    """Sortable key for 26.2.5 / 23.1.4."""
    return tuple(int(part) for part in version.split("."))


def package_version(text):
    return f"{value(text, 'pkgver')}-{value(text, 'pkgrel')}"


def published_key(version):
    """Sortable key for a published pkgver-pkgrel, with or without an epoch."""
    version = version.split(":", 1)[-1]
    pkgver, _, pkgrel = version.rpartition("-")
    return version_key(pkgver), int(pkgrel or 0)


# --- Upstream ----------------------------------------------------------------

def mesa_releases(listing):
    """Stable Mesa versions in the archive's index page (no release candidates)."""
    found = set(re.findall(r'href="mesa-(\d+\.\d+\.\d+)\.tar\.xz"', listing))
    return sorted(found, key=version_key)


def llvm_releases(releases):
    """Published, final LLVM versions from GitHub's release list."""
    versions = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        match = re.fullmatch(r"llvmorg-(\d+\.\d+\.\d+)", release.get("tag_name", ""))
        if match:
            versions.append(match.group(1))
    return sorted(set(versions), key=version_key)


def newest_llvm(current, versions):
    """(newest release of current's major, newest release of a later major)."""
    major = version_key(current)[0]
    same = [v for v in versions if version_key(v)[0] == major and version_key(v) > version_key(current)]
    later = [v for v in versions if version_key(v)[0] > major]
    return (same[-1] if same else None), (later[-1] if later else None)


def crates_from_tarball(path):
    """NVK's Rust crates, {name: version}, from Mesa's subprojects/*-rs.wrap."""
    crates = {}
    with tarfile.open(path, mode="r:*") as archive:
        for member in archive:
            if not re.fullmatch(r"[^/]+/subprojects/[^/]+-rs\.wrap", member.name):
                continue
            for line in archive.extractfile(member).read().decode().splitlines():
                match = re.match(r"\s*source_url\s*=.*crates/([^/]+)/([0-9.]+)/download", line)
                if match:
                    crates[match.group(1)] = match.group(2)
    if not crates:
        raise PkgbuildError(f"{path}: no subprojects/*-rs.wrap with a crate")
    return crates


def render_crates(crates):
    lines = [f"  {name:<16} {version}" for name, version in sorted(crates.items())]
    return "declare -A _crates=(\n" + "\n".join(lines) + "\n)"


def database_versions(data):
    """{pkgname: version} from a pacman repository database."""
    versions = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            if not member.name.endswith("/desc"):
                continue
            fields, key = {}, None
            for line in archive.extractfile(member).read().decode().splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key and key not in fields:
                    fields[key] = line
            if "NAME" in fields and "VERSION" in fields:
                versions[fields["NAME"]] = fields["VERSION"]
    return versions


# --- follow ------------------------------------------------------------------

def plan_follow(texts, mesa_versions, llvm_versions, spirv):
    """What to change, from the PKGBUILDs and what upstream and Manjaro have.

    texts: {directory: PKGBUILD text}; spirv: {directory: Manjaro's version
    of the package it links}. Returns a dict: mesa / llvm (new versions or
    None), rebuild (directories whose spirv-tools moved), llvm_major (a later
    LLVM major left to a person, or None).
    """
    pkgvers = {d: value(t, "pkgver", d) for d, t in texts.items()}
    llvmvers = {d: value(t, "_llvmver", d) for d, t in texts.items()}
    if len(set(pkgvers.values())) != 1 or len(set(llvmvers.values())) != 1:
        raise PkgbuildError(f"the PKGBUILDs are out of step: pkgver {pkgvers}, _llvmver {llvmvers}")
    current_mesa = next(iter(pkgvers.values()))
    current_llvm = next(iter(llvmvers.values()))

    newer_mesa = [v for v in mesa_versions if version_key(v) > version_key(current_mesa)]
    llvm, llvm_major = newest_llvm(current_llvm, llvm_versions)
    rebuild = sorted(
        d for d, t in texts.items()
        if spirv.get(d) and spirv[d] != value(t, "_spirvtools", d)
    )
    return {
        "from_mesa": current_mesa,
        "from_llvm": current_llvm,
        "mesa": newer_mesa[-1] if newer_mesa else None,
        "llvm": llvm,
        "llvm_major": llvm_major,
        "rebuild": rebuild,
    }


def apply_follow(texts, plan, spirv, crates=None):
    """The updated PKGBUILD texts (checksums still to be regenerated)."""
    updated = {}
    for directory, text in texts.items():
        if plan["mesa"] or plan["llvm"]:
            if plan["mesa"]:
                text = set_value(text, "pkgver", plan["mesa"])
            if plan["llvm"]:
                text = set_value(text, "_llvmver", plan["llvm"])
            text = set_value(text, "pkgrel", "1")
            if crates:
                text = replace_array(text, "declare -A _crates", render_crates(crates))
        elif directory in plan["rebuild"]:
            text = set_value(text, "pkgrel", str(int(value(text, "pkgrel", directory)) + 1))
        if spirv.get(directory):
            text = set_value(text, "_spirvtools", spirv[directory])
        updated[directory] = text
    return updated


def verify_signature(path, signature, keys):
    """Check a detached signature against the release managers' keys only."""
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, GNUPGHOME=home)
        key_files = sorted(os.path.join(keys, k) for k in os.listdir(keys) if k.endswith(".asc"))
        subprocess.run(["gpg", "--batch", "--quiet", "--import", *key_files], env=env, check=True,
                       capture_output=True)
        result = subprocess.run(["gpg", "--batch", "--status-fd", "1", "--verify", signature, path],
                                env=env, capture_output=True, text=True)
    if result.returncode != 0 or "[GNUPG:] VALIDSIG" not in result.stdout:
        raise PkgbuildError(f"{os.path.basename(path)}: bad or unknown signature\n{result.stderr.strip()}")


def download(url, directory):
    path = os.path.join(directory, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        log(f"downloading {url}")
        data = fetch(url)
        with open(path + ".part", "wb") as out:
            out.write(data)
        os.replace(path + ".part", path)
    return path


def regenerate_sums(root, directory, srcdest):
    """makepkg -g in a package directory, the downloads kept in srcdest."""
    pkgdir = os.path.join(root, directory)
    result = subprocess.run(["makepkg", "-g"], cwd=pkgdir, capture_output=True, text=True,
                            env=dict(os.environ, SRCDEST=os.path.abspath(srcdest)))
    if result.returncode != 0 or "sha256sums=(" not in result.stdout:
        raise PkgbuildError(f"{directory}: makepkg -g failed\n{result.stderr[-2000:]}")
    path = os.path.join(pkgdir, "PKGBUILD")
    with open(path, encoding="utf-8") as source:
        text = source.read()
    block = result.stdout[result.stdout.index("sha256sums=("):].strip()
    with open(path, "w", encoding="utf-8") as out:
        out.write(replace_array(text, "sha256sums", block))


def follow(root, cache, result_path):
    texts = {}
    for directory in PACKAGES:
        with open(os.path.join(root, directory, "PKGBUILD"), encoding="utf-8") as source:
            texts[directory] = source.read()
    mesa_versions = mesa_releases(fetch(MESA_ARCHIVE).decode())
    llvm_versions = llvm_releases(json.loads(fetch(LLVM_RELEASES)))
    spirv = {}
    for directory, package in PACKAGES.items():
        spirv[directory] = database_versions(fetch(MANJARO_DBS[package])).get(package)
        if not spirv[directory]:
            raise PkgbuildError(f"{package} not found in Manjaro stable")

    plan = plan_follow(texts, mesa_versions, llvm_versions, spirv)
    status = "none"
    crates = None
    if plan["mesa"] or plan["llvm"]:
        os.makedirs(cache, exist_ok=True)
        mesa = plan["mesa"] or plan["from_mesa"]
        llvm = plan["llvm"] or plan["from_llvm"]
        keys = os.path.join(root, KEYS)
        for url in (MESA_TARBALL.format(version=mesa), LLVM_TARBALL.format(version=llvm)):
            tarball = download(url, cache)
            verify_signature(tarball, download(url + ".sig", cache), keys)
            log(f"{os.path.basename(tarball)}: good signature")
        crates = crates_from_tarball(os.path.join(cache, f"mesa-{mesa}.tar.xz"))
        status = "bumped"
    elif plan["rebuild"]:
        status = "rebuilt"

    updated = apply_follow(texts, plan, spirv, crates)
    for directory, text in updated.items():
        if text != texts[directory]:
            with open(os.path.join(root, directory, "PKGBUILD"), "w", encoding="utf-8") as out:
                out.write(text)
    if status == "bumped":
        for directory in PACKAGES:
            regenerate_sums(root, directory, cache)

    result = dict(plan, status=status, spirv=spirv,
                  versions={d: package_version(t) for d, t in updated.items()})
    with open(result_path, "w", encoding="utf-8") as out:
        json.dump(result, out, indent=2)
    log(json.dumps(result, indent=2))


# --- dispatch ----------------------------------------------------------------

# As in big-kernel: most failed builds are the network, so a version that
# does not publish is sent again RETRY_AFTER after the last attempt, up to
# MAX_ATTEMPTS. A build still queued or running is never sent again.
MAX_ATTEMPTS = 3
RETRY_AFTER = 30 * 60


def behind(texts, published):
    """[(directory, version)] whose PKGBUILD is ahead of testing."""
    builds = []
    for directory, text in texts.items():
        expected = package_version(text)
        have = published.get(directory)
        if have is None or published_key(expected) > published_key(have):
            builds.append((directory, expected))
    return builds


def attempt(state, key):
    """(attempts, last dispatch as epoch seconds, gave_up) recorded for key."""
    entry = state.get(key)
    if entry is None:
        return 0, None, False
    at = calendar.timegm(time.strptime(entry["at"], "%Y-%m-%dT%H:%M:%SZ"))
    return entry.get("attempts", 1), at, entry.get("gave_up", False)


def state_key(directory, version):
    return f"testing/{directory}/{version}"


def pending(builds, state, force=False, now=None):
    """Split the builds behind into (to dispatch, given up)."""
    now = time.time() if now is None else now
    todo, given_up = [], []
    for build in builds:
        attempts, at, _gave_up = attempt(state, state_key(*build))
        if force or attempts == 0:
            todo.append(build)
        elif now - at < RETRY_AFTER:
            continue
        elif attempts < MAX_ATTEMPTS:
            todo.append(build)
        else:
            given_up.append(build)
    return todo, given_up


def payload(directory):
    """The dispatch gitrepo sends for a package, pointed at one directory."""
    return {
        "event_type": directory,
        "client_payload": {
            "branch": "main",
            "branch_type": "testing",
            "build_env": "normal",
            "url": REPOSITORY_URL,
            "tmate": False,
            "pkgbuild_dir": directory,
            "manjaro_branch": "stable",
            "new_branch": "main",
        },
    }


def github(url, token, body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read()


def check_token(token, request=None):
    """Fail clearly when the dispatch token was revoked or has expired.

    Checked on every run, not only when something is to be built: a
    fine-grained token expires, and a new Mesa release is no time to find out.
    """
    request = request or github
    try:
        request(f"https://api.github.com/repos/{WORKFLOW_REPOSITORY}", token)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise SystemExit("DISPATCH_TOKEN was revoked or has expired: create a new one "
                             "(see the README) and save it in the repository's secrets") from error
        raise
    log(f"DISPATCH_TOKEN: accepted by GitHub, reaches {WORKFLOW_REPOSITORY}")


def building(token):
    """Packages build-package is building or has queued right now."""
    names = set()
    for status in ("queued", "in_progress", "waiting", "pending"):
        _code, data = github(
            f"https://api.github.com/repos/{WORKFLOW_REPOSITORY}/actions/runs?status={status}&per_page=100",
            token)
        names.update(run.get("display_title", "") for run in json.loads(data).get("workflow_runs", []))
    return names


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as out:
        json.dump(state, out, indent=2, sort_keys=True)


def dispatch_behind(root, state_path, dry_run=False, force=False):
    texts = {}
    for directory in PACKAGES:
        with open(os.path.join(root, directory, "PKGBUILD"), encoding="utf-8") as source:
            texts[directory] = source.read()
    published = {}
    for url in TESTING_VIEW:
        published.update(database_versions(fetch(url)))
    builds = behind(texts, published)
    for directory, expected in builds:
        log(f"{directory}: PKGBUILD {expected}, testing has {published.get(directory, 'nothing')}")

    try:
        with open(state_path, encoding="utf-8") as source:
            state = json.load(source)
    except (OSError, ValueError):
        state = {}
    todo, given_up = pending(builds, state, force)
    newly_given_up = []
    for build in given_up:
        key = state_key(*build)
        log(f"{build[0]} {build[1]}: still not published after {MAX_ATTEMPTS} attempts, given up")
        if not attempt(state, key)[2]:
            newly_given_up.append(build)
            if not dry_run:
                state[key] = dict(state[key], gave_up=True)

    token = os.environ.get("DISPATCH_TOKEN", "")
    if token:
        check_token(token)
    active = building(token) if todo and not dry_run else set()
    dispatched = 0
    for directory, expected in todo:
        if directory in active:
            log(f"{directory}: a build of it is already queued or running, not dispatched again")
            continue
        body = payload(directory)
        if dry_run:
            log(f"would dispatch {directory} {expected}: {json.dumps(body['client_payload'])}")
            continue
        if not token:
            raise SystemExit("DISPATCH_TOKEN is not set")
        key = state_key(directory, expected)
        attempts = 1 if force else attempt(state, key)[0] + 1
        status, _data = github(f"https://api.github.com/repos/{WORKFLOW_REPOSITORY}/dispatches", token, body)
        log(f"dispatched {directory} {expected}, attempt {attempts} of {MAX_ATTEMPTS} (HTTP {status})")
        state[key] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "attempts": attempts}
        dispatched += 1
        save_state(state_path, state)

    if not dry_run:
        save_state(state_path, state)
    log(f"{len(builds)} package(s) behind, {dispatched} dispatched")
    if newly_given_up:
        # Fails this run once, so GitHub mails it; later runs only log it.
        names = ", ".join(f"{d} {v}" for d, v in newly_given_up)
        print(f"::error::not published after {MAX_ATTEMPTS} attempts: {names}", flush=True)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="the big-mesa repository")
    sub = parser.add_subparsers(dest="command", required=True)
    follow_cmd = sub.add_parser("follow", help="move the PKGBUILDs to new releases")
    follow_cmd.add_argument("--cache", default="mesa-cache", help="downloaded tarballs")
    follow_cmd.add_argument("--result", default="follow.json", help="what was done, as JSON")
    dispatch_cmd = sub.add_parser("dispatch", help="send what testing lacks to be built")
    dispatch_cmd.add_argument("--state", default="watch-state.json", help="builds already dispatched")
    dispatch_cmd.add_argument("--dry-run", action="store_true", help="report, dispatch nothing")
    dispatch_cmd.add_argument("--force", action="store_true", help="dispatch even what was dispatched before")
    args = parser.parse_args()

    if args.command == "follow":
        follow(args.root, args.cache, args.result)
        return 0
    return dispatch_behind(args.root, args.state, args.dry_run, args.force)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PkgbuildError as error:
        sys.exit(f"error: {error}")

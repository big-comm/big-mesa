"""Tests for watch_mesa.py, against the repository's real PKGBUILDs."""

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watch_mesa  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(directory):
    with open(os.path.join(ROOT, directory, "PKGBUILD"), encoding="utf-8") as source:
        return source.read()


def texts():
    return {directory: read(directory) for directory in watch_mesa.PACKAGES}


def current(key):
    return watch_mesa.value(read("mesa-big"), key)


def spirv_now():
    return {d: watch_mesa.value(t, "_spirvtools") for d, t in texts().items()}


class TestUpstream(unittest.TestCase):
    def test_mesa_release_candidates_are_ignored(self):
        listing = "".join(
            f'<a href="mesa-{v}.tar.xz">' for v in ("26.2.10", "26.2.9", "26.3.0-rc2", "26.2.9.sig")
        ) + '<a href="mesa-26.2.9.tar.xz.sig">'
        self.assertEqual(watch_mesa.mesa_releases(listing), ["26.2.9", "26.2.10"])

    def test_llvm_drafts_and_release_candidates_are_ignored(self):
        releases = [
            {"tag_name": "llvmorg-24.1.0-rc1", "prerelease": True},
            {"tag_name": "llvmorg-23.1.5", "draft": True},
            {"tag_name": "llvmorg-23.1.4"},
            {"tag_name": "llvmorg-22.1.8"},
        ]
        self.assertEqual(watch_mesa.llvm_releases(releases), ["22.1.8", "23.1.4"])

    def test_a_new_llvm_major_is_reported_not_followed(self):
        same, later = watch_mesa.newest_llvm("23.1.4", ["23.1.4", "23.1.5", "24.1.0"])
        self.assertEqual((same, later), ("23.1.5", "24.1.0"))


class TestPlan(unittest.TestCase):
    def plan(self, mesa=(), llvm=(), spirv=None):
        return watch_mesa.plan_follow(texts(), list(mesa), list(llvm), spirv or spirv_now())

    def test_nothing_new(self):
        plan = self.plan([current("pkgver")], [current("_llvmver")])
        self.assertIsNone(plan["mesa"])
        self.assertIsNone(plan["llvm"])
        self.assertEqual(plan["rebuild"], [])

    def test_a_new_series_dot_zero_is_followed(self):
        plan = self.plan([current("pkgver"), "99.0.0"])
        self.assertEqual(plan["mesa"], "99.0.0")

    def test_spirv_tools_moving_rebuilds_only_that_package(self):
        spirv = spirv_now()
        spirv["lib32-mesa-big"] = "1:9.9.9-1"
        plan = self.plan(spirv=spirv)
        self.assertEqual(plan["rebuild"], ["lib32-mesa-big"])

    def test_pkgbuilds_out_of_step_are_refused(self):
        out = texts()
        out["lib32-mesa-big"] = watch_mesa.set_value(out["lib32-mesa-big"], "pkgver", "1.0.0")
        with self.assertRaises(watch_mesa.PkgbuildError):
            watch_mesa.plan_follow(out, [], [], spirv_now())


class TestApply(unittest.TestCase):
    def test_bump_moves_both_together(self):
        plan = watch_mesa.plan_follow(texts(), ["99.0.0"], ["23.9.9"], spirv_now())
        crates = {"bitflags": "9.9.9", "syn": "3.0.0"}
        updated = watch_mesa.apply_follow(texts(), plan, spirv_now(), crates)
        for directory, text in updated.items():
            with self.subTest(directory=directory):
                self.assertEqual(watch_mesa.value(text, "pkgver"), "99.0.0")
                self.assertEqual(watch_mesa.value(text, "_llvmver"), "23.9.9")
                self.assertEqual(watch_mesa.value(text, "pkgrel"), "1")
                self.assertIn("declare -A _crates=(\n  bitflags         9.9.9\n  syn              3.0.0\n)", text)
                self.assertEqual(self.bash_check(text), "ok")

    def test_rebuild_raises_pkgrel_and_records_spirv_tools(self):
        spirv = spirv_now()
        spirv["mesa-big"] = "1:9.9.9-1"
        before = texts()
        plan = watch_mesa.plan_follow(before, [], [], spirv)
        updated = watch_mesa.apply_follow(before, plan, spirv)
        rel = int(watch_mesa.value(before["mesa-big"], "pkgrel"))
        self.assertEqual(watch_mesa.value(updated["mesa-big"], "pkgrel"), str(rel + 1))
        self.assertEqual(watch_mesa.value(updated["mesa-big"], "_spirvtools"), "1:9.9.9-1")
        self.assertEqual(updated["lib32-mesa-big"], before["lib32-mesa-big"])

    def test_crate_list_renders_as_written_in_the_pkgbuilds(self):
        # The watcher's rendering must not reformat the list it did not change.
        for directory, text in texts().items():
            block = re.search(r"^declare -A _crates=\(\n.*?\n\)", text, re.MULTILINE | re.DOTALL).group(0)
            crates = dict(line.split() for line in block.splitlines()[1:-1])
            with self.subTest(directory=directory):
                self.assertEqual(watch_mesa.render_crates(crates), block)

    def test_both_pkgbuilds_carry_the_same_crates(self):
        blocks = {re.search(r"^declare -A _crates=\(.*?\)", t, re.MULTILINE | re.DOTALL).group(0)
                  for t in texts().values()}
        self.assertEqual(len(blocks), 1)

    def test_sums_are_replaced_whole(self):
        text = watch_mesa.replace_array(read("mesa-big"), "sha256sums", "sha256sums=('x')")
        self.assertIn("sha256sums=('x')\n", text)
        self.assertNotIn("794b3d17", text)
        self.assertEqual(self.bash_check(text), "ok")

    @staticmethod
    def bash_check(text):
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as script:
            script.write(text)
            script.flush()
            result = subprocess.run(["bash", "-n", script.name], capture_output=True, text=True)
        return "ok" if result.returncode == 0 else result.stderr


class TestCrates(unittest.TestCase):
    def test_from_the_wrap_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mesa-99.0.0.tar.xz")
            with tarfile.open(path, "w:xz") as archive:
                for name, body in {
                    "mesa-99.0.0/subprojects/syn-rs.wrap":
                        "[wrap-file]\nsource_url = https://crates.io/api/v1/crates/syn/2.0.87/download\n",
                    "mesa-99.0.0/subprojects/packagefiles/syn/meson.build": "project('syn')\n",
                    "mesa-99.0.0/subprojects/expat.wrap": "source_url = https://example.org/expat.tar.xz\n",
                }.items():
                    data = body.encode()
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            self.assertEqual(watch_mesa.crates_from_tarball(path), {"syn": "2.0.87"})


@unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
class TestSignature(unittest.TestCase):
    def test_only_the_release_managers_keys_are_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "gnupg")
            os.mkdir(home, 0o700)
            env = dict(os.environ, GNUPGHOME=home)
            subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key",
                            "Someone <someone@example.org>", "ed25519", "sign", "never"],
                           env=env, check=True, capture_output=True)
            tarball = os.path.join(tmp, "mesa.tar.xz")
            with open(tarball, "wb") as out:
                out.write(b"not really mesa")
            subprocess.run(["gpg", "--batch", "--detach-sign", "-o", tarball + ".sig", tarball],
                           env=env, check=True, capture_output=True)

            # Signed, but not by a key in keys/pgp.
            with self.assertRaises(watch_mesa.PkgbuildError):
                watch_mesa.verify_signature(tarball, tarball + ".sig", os.path.join(ROOT, watch_mesa.KEYS))

            keys = os.path.join(tmp, "keys")
            os.mkdir(keys)
            exported = subprocess.run(["gpg", "--armor", "--export"], env=env, check=True,
                                      capture_output=True).stdout
            with open(os.path.join(keys, "someone.asc"), "wb") as out:
                out.write(exported)
            watch_mesa.verify_signature(tarball, tarball + ".sig", keys)

            with open(tarball, "ab") as out:
                out.write(b"tampered")
            with self.assertRaises(watch_mesa.PkgbuildError):
                watch_mesa.verify_signature(tarball, tarball + ".sig", keys)

    def test_the_keys_directory_holds_the_pkgbuilds_keys(self):
        names = sorted(os.listdir(os.path.join(ROOT, watch_mesa.KEYS)))
        self.assertTrue(names)
        self.assertTrue(all(re.fullmatch(r"[0-9A-F]{40}\.asc", n) for n in names), names)


class TestDispatch(unittest.TestCase):
    def test_behind_compares_with_testing(self):
        here = {d: watch_mesa.package_version(t) for d, t in texts().items()}
        published = {d: "1:" + v for d, v in here.items()}
        self.assertEqual(watch_mesa.behind(texts(), published), [])
        published["lib32-mesa-big"] = "0.1-1"
        self.assertEqual(watch_mesa.behind(texts(), published), [("lib32-mesa-big", here["lib32-mesa-big"])])
        del published["mesa-big"]
        self.assertIn(("mesa-big", here["mesa-big"]), watch_mesa.behind(texts(), published))

    def test_a_higher_pkgrel_is_ahead(self):
        self.assertGreater(watch_mesa.published_key("26.2.5-2"), watch_mesa.published_key("26.2.5-1"))
        self.assertGreater(watch_mesa.published_key("26.2.10-1"), watch_mesa.published_key("26.2.9-3"))

    def test_retries_up_to_three_attempts(self):
        build = ("mesa-big", "26.2.6-1")
        key = watch_mesa.state_key(*build)
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        self.assertEqual(watch_mesa.pending([build], {}, now=now), ([build], []))
        self.assertEqual(watch_mesa.pending([build], {key: {"at": recent, "attempts": 1}}, now=now), ([], []))
        self.assertEqual(watch_mesa.pending([build], {key: {"at": stamp, "attempts": 2}}, now=now), ([build], []))
        self.assertEqual(watch_mesa.pending([build], {key: {"at": stamp, "attempts": 3}}, now=now), ([], [build]))
        self.assertEqual(
            watch_mesa.pending([build], {key: {"at": recent, "attempts": 3}}, force=True, now=now), ([build], []))

    def test_an_expired_token_is_reported_clearly(self):
        def expired(url, token):
            raise watch_mesa.urllib.error.HTTPError(url, 401, "Bad credentials", {}, None)

        with self.assertRaises(SystemExit) as raised:
            watch_mesa.check_token("old", expired)
        self.assertIn("revoked or has expired", str(raised.exception))
        watch_mesa.check_token("good", lambda url, token: (200, b"{}"))

    def test_payload_names_the_directory(self):
        for directory in watch_mesa.PACKAGES:
            body = watch_mesa.payload(directory)
            with self.subTest(directory=directory):
                self.assertEqual(body["event_type"], directory)
                self.assertEqual(body["client_payload"]["pkgbuild_dir"], directory)
                self.assertEqual(body["client_payload"]["branch_type"], "testing")


if __name__ == "__main__":
    unittest.main()

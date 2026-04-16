"""
Unit tests for .github/scripts/update-plugin-packages.py

The script filename contains a hyphen, so it is loaded via importlib.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "update-plugin-packages.py"


def _load_updater():
    name = "update_plugin_packages"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Required so dataclasses (and similar) can resolve the module namespace on 3.12+.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


up = _load_updater()


def _minimal_plg(
    *,
    plugin_version: str = "2020.01.01",
    vim_bn: str = "vim-8.2.1000-x86_64-1.txz",
    sod_bn: str = "libsodium-1.0.17-x86_64-1.txz",
    lac_ver: str = "1.0.5",
) -> str:
    return f"""<?xml version='1.0' standalone='yes'?>
<!DOCTYPE PLUGIN [
<!ENTITY name "vim">
<!ENTITY author "test">
<!ENTITY version "{plugin_version}">
<!ENTITY pluginURL "https://example.invalid/plugin.plg">
<!ENTITY pluginLOC "/boot/x">
<!ENTITY emhttpLOC "/usr/x">
]>
<PLUGIN name="&name;" author="&author;" version="&version;" pluginURL="&pluginURL;">
<CHANGES>
<![CDATA[
###{plugin_version}###
- prior entry
]]>
</CHANGES>
<FILE Name="/boot/config/plugins/vim/{vim_bn}" Run="upgradepkg --install-new">
<URL>https://example.invalid/old-vim</URL>
<SHA256>aa</SHA256>
</FILE>
<FILE Name="/boot/config/plugins/vim/{sod_bn}" Run="upgradepkg --install-new">
<URL>https://example.invalid/old-so</URL>
<SHA256>bb</SHA256>
</FILE>
<FILE Name="/boot/config/plugins/vim/libagentcrypt-{lac_ver}.tgz" Run="installpkg">
<URL>https://example.invalid/old-lac.tar.gz</URL>
<SHA256>cc</SHA256>
</FILE>
</PLUGIN>
"""


class FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body
        self._chunks: list[bytes] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, n: int = -1):
        if self._chunks is not None:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)
        if n == -1:
            return self._body
        if not self._body:
            return b""
        out, self._body = self._body[:n], self._body[n:]
        return out


def _fake_urlopen_factory(url_map: dict[str, bytes], stream_chunks: dict[str, list[bytes]]):
    def fake_urlopen(req, timeout=None):
        full = req.full_url
        if full in stream_chunks:
            r = FakeHTTPResponse(b"")
            r._chunks = list(stream_chunks[full])
            return r
        if full not in url_map:
            raise AssertionError(f"unexpected URL in fake_urlopen: {full}")
        return FakeHTTPResponse(url_map[full])

    return fake_urlopen


class TestVersionParsing(unittest.TestCase):
    def test_parse_vim_basename_orders_patch(self):
        a = up._parse_vim_basename("vim-8.2.1000-x86_64-1.txz")
        b = up._parse_vim_basename("vim-8.2.2000-x86_64-1.txz")
        self.assertLess(a, b)

    def test_parse_vim_basename_build_revision(self):
        a = up._parse_vim_basename("vim-8.2.1000-x86_64-1.txz")
        b = up._parse_vim_basename("vim-8.2.1000-x86_64-2.txz")
        self.assertLess(a, b)

    def test_parse_libsodium_basename(self):
        a = up._parse_libsodium_basename("libsodium-1.0.17-x86_64-1.txz")
        b = up._parse_libsodium_basename("libsodium-1.0.18-x86_64-1.txz")
        self.assertLess(a, b)


class TestFindLatestSlackware(unittest.TestCase):
    def test_picks_highest_vim_from_index_html(self):
        html = """
        <html><body>
        <a href="vim-8.2.1000-x86_64-1.txz">a</a>
        <a href="vim-8.2.2000-x86_64-1.txz">b</a>
        </body></html>
        """

        with patch.object(up, "_fetch_text", return_value=html):
            got = up._find_latest_slackware_txz("ap", "vim-")
        self.assertEqual(got, "vim-8.2.2000-x86_64-1.txz")

    def test_picks_highest_libsodium(self):
        html = """
        <a href="libsodium-1.0.17-x86_64-9.txz"></a>
        <a href="libsodium-1.0.18-x86_64-1.txz"></a>
        """

        with patch.object(up, "_fetch_text", return_value=html):
            got = up._find_latest_slackware_txz("l", "libsodium-")
        self.assertEqual(got, "libsodium-1.0.18-x86_64-1.txz")

    def test_raises_when_no_matches(self):
        with patch.object(up, "_fetch_text", return_value="<html></html>"):
            with self.assertRaises(RuntimeError):
                up._find_latest_slackware_txz("ap", "vim-")


class TestLibagentcryptTag(unittest.TestCase):
    def test_picks_latest_semver_tag_across_pages(self):
        page1 = [
            {"name": "v1.0.5"},
            {"name": "v1.0.6"},
            {"name": "upstream/1.0.6"},
        ]
        page2: list[dict] = []

        bodies = {
            "https://api.github.com/repos/ndilieto/libagentcrypt/tags?per_page=100&page=1": json.dumps(
                page1
            ).encode(),
            "https://api.github.com/repos/ndilieto/libagentcrypt/tags?per_page=100&page=2": json.dumps(
                page2
            ).encode(),
        }

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_factory(bodies, {})):
            self.assertEqual(up._latest_libagentcrypt_tag(), "v1.0.6")

    def test_raises_when_no_semver_tags(self):
        bodies = {
            "https://api.github.com/repos/ndilieto/libagentcrypt/tags?per_page=100&page=1": json.dumps(
                [{"name": "debian/1.0.6-1"}]
            ).encode(),
        }
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_factory(bodies, {})):
            with self.assertRaises(RuntimeError):
                up._latest_libagentcrypt_tag()


class TestDisplayAndUrlHelpers(unittest.TestCase):
    def test_vim_display_version(self):
        self.assertEqual(
            up._vim_display_version("vim-8.2.4256-x86_64-1.txz"),
            "8.2.4256",
        )

    def test_libsodium_display(self):
        self.assertEqual(
            up._libsodium_display("libsodium-1.0.18-x86_64-3.txz"),
            "1.0.18 (rev 3)",
        )

    def test_libagentcrypt_url(self):
        self.assertEqual(
            up._libagentcrypt_url("v1.0.6"),
            "https://github.com/ndilieto/libagentcrypt/archive/v1.0.6/libagentcrypt-1.0.6.tar.gz",
        )


class TestParseCurrent(unittest.TestCase):
    def test_parse_current_success(self):
        plg = _minimal_plg()
        cur = up._parse_current(plg)
        self.assertEqual(cur["plugin_version"], "2020.01.01")
        self.assertEqual(cur["vim_basename"], "vim-8.2.1000-x86_64-1.txz")
        self.assertEqual(cur["libsodium_basename"], "libsodium-1.0.17-x86_64-1.txz")
        self.assertEqual(cur["libagentcrypt_ver"], "1.0.5")


class TestApplyUpdates(unittest.TestCase):
    def test_prepends_changes_and_updates_files(self):
        plg = _minimal_plg()
        vim = up.SlackwarePkg(
            subdir="ap",
            prefix="vim-",
            basename="vim-8.2.2000-x86_64-1.txz",
            url="https://mirrors.example/ap/vim-8.2.2000-x86_64-1.txz",
            sha256="11" * 32,
        )
        sod = up.SlackwarePkg(
            subdir="l",
            prefix="libsodium-",
            basename="libsodium-1.0.18-x86_64-3.txz",
            url="https://mirrors.example/l/libsodium-1.0.18-x86_64-3.txz",
            sha256="22" * 32,
        )
        out = up._apply_updates(
            plg,
            plugin_date="2099.01.01",
            vim=vim,
            libsodium=sod,
            libagentcrypt_ver="1.0.6",
            libagentcrypt_url="https://github.com/x/archive/v1.0.6/libagentcrypt-1.0.6.tar.gz",
            libagentcrypt_sha256="33" * 32,
        )
        self.assertIn('<!ENTITY version "2099.01.01"', out)
        self.assertIn("###2099.01.01###", out)
        self.assertIn("- Vim version 8.2.2000", out)
        self.assertIn("###2020.01.01###", out)
        self.assertIn("- prior entry", out)
        self.assertIn("vim-8.2.2000-x86_64-1.txz", out)
        self.assertIn("libsodium-1.0.18-x86_64-3.txz", out)
        self.assertIn("libagentcrypt-1.0.6.tgz", out)
        self.assertNotIn("libagentcrypt-1.0.5.tgz", out)
        self.assertIn("11" * 32, out)


class TestConventionalCommitSubject(unittest.TestCase):
    def test_lists_only_changed_packages(self):
        old = {
            "vim_basename": "vim-8.2.1000-x86_64-1.txz",
            "libsodium_basename": "libsodium-1.0.17-x86_64-1.txz",
            "libagentcrypt_ver": "1.0.5",
        }
        subj = up._conventional_commit_subject(
            old=old,
            vim_bn="vim-8.2.2000-x86_64-1.txz",
            sod_bn="libsodium-1.0.17-x86_64-1.txz",
            lac_ver="1.0.5",
        )
        self.assertTrue(subj.startswith("chore(plugin): update packages ("))
        self.assertIn("8.2.1000", subj)
        self.assertIn("8.2.2000", subj)
        self.assertNotIn("libsodium", subj)

    def test_truncates_long_subject(self):
        old = {
            "vim_basename": "vim-1.0.0-x86_64-1.txz",
            "libsodium_basename": "libsodium-1.0.0-x86_64-1.txz",
            "libagentcrypt_ver": "1.0.0",
        }
        subj = up._conventional_commit_subject(
            old=old,
            vim_bn="vim-9.9.9-x86_64-1.txz",
            sod_bn="libsodium-9.9.9-x86_64-9.txz",
            lac_ver="9.9.9",
        )
        self.assertLessEqual(len(subj), 72)
        self.assertTrue(subj.endswith("..."))


class TestMain(unittest.TestCase):
    def test_main_noop_when_pins_match(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "_test_tmp_main"
        root.mkdir(exist_ok=True)
        try:
            plg_dir = root / "plugin"
            plg_dir.mkdir(exist_ok=True)
            plg_path = plg_dir / "vim.plg"
            msg_path = root / ".github" / "commit-message.txt"
            msg_path.parent.mkdir(parents=True, exist_ok=True)
            msg_path.write_text("should-be-removed\n", encoding="utf-8")

            plg_path.write_text(
                _minimal_plg(
                    vim_bn="vim-8.2.2000-x86_64-1.txz",
                    sod_bn="libsodium-1.0.18-x86_64-3.txz",
                    lac_ver="1.0.6",
                ),
                encoding="utf-8",
            )

            index_ap = b'<a href="vim-8.2.2000-x86_64-1.txz"></a>'
            index_l = b'<a href="libsodium-1.0.18-x86_64-3.txz"></a>'
            tags = json.dumps([{"name": "v1.0.6"}]).encode()

            url_map = {
                f"{up.SLACKWARE_BASE}/ap/": index_ap,
                f"{up.SLACKWARE_BASE}/l/": index_l,
                "https://api.github.com/repos/ndilieto/libagentcrypt/tags?per_page=100&page=1": tags,
            }

            with patch.object(up, "PLG_PATH", plg_path):
                with patch.object(up, "COMMIT_MSG_PATH", msg_path):
                    with patch.object(up, "REPO_ROOT", root):
                        with patch(
                            "urllib.request.urlopen",
                            side_effect=_fake_urlopen_factory(url_map, {}),
                        ):
                            with patch("builtins.print"):
                                rc = up.main()
            self.assertEqual(rc, 0)
            self.assertFalse(msg_path.exists())
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_main_writes_plg_and_commit_message_on_upgrade(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "_test_tmp_main2"
        root.mkdir(exist_ok=True)
        try:
            plg_dir = root / "plugin"
            plg_dir.mkdir(exist_ok=True)
            plg_path = plg_dir / "vim.plg"
            msg_path = root / ".github" / "commit-message.txt"

            plg_path.write_text(_minimal_plg(), encoding="utf-8")

            index_ap = b'<a href="vim-8.2.2000-x86_64-1.txz"></a>'
            index_l = b'<a href="libsodium-1.0.18-x86_64-3.txz"></a>'
            tags = json.dumps([{"name": "v1.0.6"}]).encode()
            vim_url = f"{up.SLACKWARE_BASE}/ap/vim-8.2.2000-x86_64-1.txz"
            sod_url = f"{up.SLACKWARE_BASE}/l/libsodium-1.0.18-x86_64-3.txz"
            lac_url = up._libagentcrypt_url("v1.0.6")

            tiny = b"x" * 2048
            vim_sha = hashlib.sha256(tiny).hexdigest()
            sod_sha = hashlib.sha256(tiny + b"y").hexdigest()
            lac_sha = hashlib.sha256(tiny + b"z").hexdigest()

            url_map = {
                f"{up.SLACKWARE_BASE}/ap/": index_ap,
                f"{up.SLACKWARE_BASE}/l/": index_l,
                "https://api.github.com/repos/ndilieto/libagentcrypt/tags?per_page=100&page=1": tags,
                vim_url: tiny,
                sod_url: tiny + b"y",
                lac_url: tiny + b"z",
            }
            streams = {
                vim_url: [tiny],
                sod_url: [tiny + b"y"],
                lac_url: [tiny + b"z"],
            }

            with patch.object(up, "PLG_PATH", plg_path):
                with patch.object(up, "COMMIT_MSG_PATH", msg_path):
                    with patch.object(up, "REPO_ROOT", root):
                        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_factory(url_map, streams)):
                            with patch.object(
                                up,
                                "_utc_plugin_date_str",
                                return_value="2030.06.15",
                            ):
                                with patch("builtins.print"):
                                    rc = up.main()
            self.assertEqual(rc, 0)
            new_plg = plg_path.read_text(encoding="utf-8")
            self.assertIn('<!ENTITY version "2030.06.15"', new_plg)
            self.assertIn(vim_sha, new_plg)
            self.assertIn(sod_sha, new_plg)
            self.assertIn(lac_sha, new_plg)
            self.assertTrue(msg_path.exists())
            commit_txt = msg_path.read_text(encoding="utf-8")
            self.assertIn("chore(plugin):", commit_txt)
            self.assertIn("libagentcrypt", commit_txt)
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

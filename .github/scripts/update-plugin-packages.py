#!/usr/bin/env python3
"""
Refresh Slackware 15.0 + libagentcrypt pins in plugin/vim.plg.
Writes .github/commit-message.txt when updates are applied (for CI).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SLACKWARE_BASE = (
    "https://mirrors.slackware.com/slackware/slackware64-15.0/slackware64"
)
LIBAGENTCRYPT_REPO = "ndilieto/libagentcrypt"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLG_PATH = REPO_ROOT / "plugin" / "vim.plg"
COMMIT_MSG_PATH = REPO_ROOT / ".github" / "commit-message.txt"


@dataclass(frozen=True)
class SlackwarePkg:
    subdir: str
    prefix: str
    basename: str
    url: str
    sha256: str

    @property
    def boot_name(self) -> str:
        return f"/boot/config/plugins/vim/{self.basename}"


def _fetch_text(url: str, timeout: int = 120) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "unraid-vim-plugin-updater/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _sha256_url(url: str, timeout: int = 600) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "unraid-vim-plugin-updater/1.0"},
    )
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _version_tuple_from_segments(s: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in s.split("."):
        if p.isdigit():
            parts.append(int(p))
        else:
            parts.append(-1)
    return tuple(parts)


def _parse_vim_basename(basename: str) -> tuple[tuple[int, ...], int]:
    m = re.match(r"vim-(.+)-x86_64-(\d+)\.txz$", basename)
    if not m:
        raise ValueError(f"unexpected vim package name: {basename}")
    return _version_tuple_from_segments(m.group(1)), int(m.group(2))


def _parse_libsodium_basename(basename: str) -> tuple[tuple[int, ...], int]:
    m = re.match(r"libsodium-(\d+\.\d+\.\d+)-x86_64-(\d+)\.txz$", basename)
    if not m:
        raise ValueError(f"unexpected libsodium package name: {basename}")
    return _version_tuple_from_segments(m.group(1)), int(m.group(2))


def _find_latest_slackware_txz(subdir: str, prefix: str) -> str:
    index_url = f"{SLACKWARE_BASE}/{subdir}/"
    html = _fetch_text(index_url)
    pat = re.compile(
        rf'href="({re.escape(prefix)}[^"]*?-x86_64-\d+\.txz)"',
        re.IGNORECASE,
    )
    found = pat.findall(html)
    if not found:
        raise RuntimeError(f"no {prefix}*.txz found under {index_url}")

    def sort_key(name: str) -> tuple:
        if name.startswith("vim-"):
            return _parse_vim_basename(name)
        if name.startswith("libsodium-"):
            return _parse_libsodium_basename(name)
        raise ValueError(name)

    return max(found, key=sort_key)


def _slackware_pkg(subdir: str, prefix: str, basename: str) -> SlackwarePkg:
    url = f"{SLACKWARE_BASE}/{subdir}/{basename}"
    digest = _sha256_url(url)
    return SlackwarePkg(
        subdir=subdir,
        prefix=prefix,
        basename=basename,
        url=url,
        sha256=digest,
    )


def _latest_libagentcrypt_tag() -> str:
    semver_tags: list[tuple[tuple[int, int, int], str]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{LIBAGENTCRYPT_REPO}/tags"
            f"?per_page=100&page={page}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "unraid-vim-plugin-updater/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list) or not data:
            break
        for t in data:
            if not isinstance(t, dict):
                continue
            n = t.get("name")
            if not isinstance(n, str):
                continue
            m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", n)
            if m:
                semver_tags.append(
                    (tuple(int(x) for x in m.groups()), n),
                )
        if len(data) < 100:
            break
        page += 1
    if not semver_tags:
        raise RuntimeError(f"no vX.Y.Z tags found for {LIBAGENTCRYPT_REPO}")
    semver_tags.sort(key=lambda x: x[0])
    return semver_tags[-1][1]


def _libagentcrypt_url(tag: str) -> str:
    ver = tag.lstrip("v")
    return (
        f"https://github.com/{LIBAGENTCRYPT_REPO}/archive/"
        f"{tag}/libagentcrypt-{ver}.tar.gz"
    )


def _read_plg() -> str:
    return PLG_PATH.read_text(encoding="utf-8")


def _parse_current(plg: str) -> dict[str, str]:
    out: dict[str, str] = {}
    m = re.search(r'<!ENTITY version "([^"]+)"', plg)
    if not m:
        sys.exit("could not parse plugin ENTITY version")
    out["plugin_version"] = m.group(1)

    m = re.search(
        r'<FILE Name="/boot/config/plugins/vim/(vim-[^"]+\.txz)"',
        plg,
    )
    if not m:
        sys.exit("could not parse current vim txz name")
    out["vim_basename"] = m.group(1)

    m = re.search(
        r'<FILE Name="/boot/config/plugins/vim/(libsodium-[^"]+\.txz)"',
        plg,
    )
    if not m:
        sys.exit("could not parse current libsodium txz name")
    out["libsodium_basename"] = m.group(1)

    m = re.search(
        r'<FILE Name="/boot/config/plugins/vim/libagentcrypt-([^"]+)\.tgz"',
        plg,
    )
    if not m:
        sys.exit("could not parse current libagentcrypt version")
    out["libagentcrypt_ver"] = m.group(1)

    return out


def _vim_display_version(basename: str) -> str:
    m = re.match(r"vim-(.+)-x86_64-\d+\.txz$", basename)
    return m.group(1) if m else basename


def _libsodium_display(basename: str) -> str:
    m = re.match(r"libsodium-(\d+\.\d+\.\d+)-x86_64-(\d+)\.txz$", basename)
    if not m:
        return basename
    return f"{m.group(1)} (rev {m.group(2)})"


def _utc_plugin_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y.%m.%d")


def _apply_updates(
    plg: str,
    *,
    plugin_date: str,
    vim: SlackwarePkg,
    libsodium: SlackwarePkg,
    libagentcrypt_ver: str,
    libagentcrypt_url: str,
    libagentcrypt_sha256: str,
) -> str:
    plg, n = re.subn(
        r'(<!ENTITY version ")[^"]+(")',
        rf"\g<1>{plugin_date}\g<2>",
        plg,
        count=1,
    )
    if n != 1:
        sys.exit("failed to replace ENTITY version")

    new_entry = (
        f"###{plugin_date}###\n"
        f"- Vim version {_vim_display_version(vim.basename)}\n"
        f"- libsodium {_libsodium_display(libsodium.basename)}\n"
        f"- libagentcrypt {libagentcrypt_ver}\n"
    )

    def _prepend_changes(m):
        before, inner, after = m.group(1), m.group(2), m.group(3)
        prior = inner.lstrip("\n")
        combined = f"\n{new_entry}\n{prior}" if prior else f"\n{new_entry}\n"
        return before + combined + after

    plg, n = re.subn(
        r"(<CHANGES>\s*<!\[CDATA\[)(.*?)(\]\]>\s*</CHANGES>)",
        _prepend_changes,
        plg,
        count=1,
        flags=re.DOTALL,
    )
    if n != 1:
        sys.exit("failed to update CHANGES block")

    vim_block = (
        f'<FILE Name="{vim.boot_name}" Run="upgradepkg --install-new">\n'
        f"<URL>{vim.url}</URL>\n"
        f"<SHA256>{vim.sha256}</SHA256>\n"
        "</FILE>\n\n"
    )
    vim_pat = (
        r'<FILE Name="/boot/config/plugins/vim/vim-[^"]+\.txz"[^>]*>'
        r".*?</FILE>\s*"
    )
    plg, n_vim = re.subn(vim_pat, vim_block, plg, count=1, flags=re.DOTALL)
    if n_vim != 1:
        sys.exit("failed to replace vim FILE block")

    sod_block = (
        f'<FILE Name="{libsodium.boot_name}" Run="upgradepkg --install-new">\n'
        f"<URL>{libsodium.url}</URL>\n"
        f"<SHA256>{libsodium.sha256}</SHA256>\n"
        "</FILE>\n\n"
    )
    sod_pat = (
        r'<FILE Name="/boot/config/plugins/vim/libsodium-[^"]+\.txz"[^>]*>'
        r".*?</FILE>\s*"
    )
    plg, n_sod = re.subn(sod_pat, sod_block, plg, count=1, flags=re.DOTALL)
    if n_sod != 1:
        sys.exit("failed to replace libsodium FILE block")

    lac_boot = f"/boot/config/plugins/vim/libagentcrypt-{libagentcrypt_ver}.tgz"
    lac_block = (
        f'<FILE Name="{lac_boot}" Run="installpkg">\n'
        f"<URL>{libagentcrypt_url}</URL>\n"
        f"<SHA256>{libagentcrypt_sha256}</SHA256>\n"
        "</FILE>\n\n"
    )
    lac_pat = (
        r'<FILE Name="/boot/config/plugins/vim/libagentcrypt-[^"]+\.tgz"[^>]*>'
        r".*?</FILE>\s*"
    )
    plg, n_lac = re.subn(lac_pat, lac_block, plg, count=1, flags=re.DOTALL)
    if n_lac != 1:
        sys.exit("failed to replace libagentcrypt FILE block")

    return plg


def _conventional_commit_subject(
    *,
    old: dict[str, str],
    vim_bn: str,
    sod_bn: str,
    lac_ver: str,
) -> str:
    parts: list[str] = []
    if vim_bn != old["vim_basename"]:
        parts.append(f"vim {_vim_display_version(old['vim_basename'])} → {_vim_display_version(vim_bn)}")
    if sod_bn != old["libsodium_basename"]:
        parts.append(
            f"libsodium {old['libsodium_basename'].removesuffix('.txz')} → {sod_bn.removesuffix('.txz')}",
        )
    if lac_ver != old["libagentcrypt_ver"]:
        parts.append(
            f"libagentcrypt {old['libagentcrypt_ver']} → {lac_ver}",
        )
    detail = ", ".join(parts)
    subject = f"chore(plugin): update packages ({detail})"
    if len(subject) > 72:
        subject = subject[:69] + "..."
    return subject


def main() -> int:
    plg = _read_plg()
    cur = _parse_current(plg)

    vim_bn = _find_latest_slackware_txz("ap", "vim-")
    sod_bn = _find_latest_slackware_txz("l", "libsodium-")
    lac_tag = _latest_libagentcrypt_tag()
    lac_ver = lac_tag.lstrip("v")

    if (
        vim_bn == cur["vim_basename"]
        and sod_bn == cur["libsodium_basename"]
        and lac_ver == cur["libagentcrypt_ver"]
    ):
        print("plugin/vim.plg already matches latest Slackware 15.0 + libagentcrypt pins")
        if COMMIT_MSG_PATH.exists():
            COMMIT_MSG_PATH.unlink()
        return 0

    print("Resolving SHA256 checksums (downloads Slackware .txz + libagentcrypt tarball)...")
    vim_pkg = _slackware_pkg("ap", "vim-", vim_bn)
    sod_pkg = _slackware_pkg("l", "libsodium-", sod_bn)
    lac_url = _libagentcrypt_url(lac_tag)
    lac_sha = _sha256_url(lac_url)

    plugin_date = _utc_plugin_date_str()
    new_plg = _apply_updates(
        plg,
        plugin_date=plugin_date,
        vim=vim_pkg,
        libsodium=sod_pkg,
        libagentcrypt_ver=lac_ver,
        libagentcrypt_url=lac_url,
        libagentcrypt_sha256=lac_sha,
    )

    PLG_PATH.write_text(new_plg, encoding="utf-8")

    subject = _conventional_commit_subject(
        old=cur,
        vim_bn=vim_bn,
        sod_bn=sod_bn,
        lac_ver=lac_ver,
    )
    body_lines = [
        "Automated weekly dependency refresh.",
        "",
        "Updates:",
    ]
    if vim_bn != cur["vim_basename"]:
        body_lines.append(
            f"- vim: {cur['vim_basename']} → {vim_bn}",
        )
    if sod_bn != cur["libsodium_basename"]:
        body_lines.append(
            f"- libsodium: {cur['libsodium_basename']} → {sod_bn}",
        )
    if lac_ver != cur["libagentcrypt_ver"]:
        body_lines.append(
            f"- libagentcrypt: {cur['libagentcrypt_ver']} → {lac_ver}",
        )

    COMMIT_MSG_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMIT_MSG_PATH.write_text(subject + "\n\n" + "\n".join(body_lines) + "\n", encoding="utf-8")
    print(f"Wrote {PLG_PATH.relative_to(REPO_ROOT)}")
    print(f"Wrote {COMMIT_MSG_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Download official images and verify their published checksums."""
import concurrent.futures
import json
import urllib.request
from runtime import IMAGES, download, prepare_dirs

OPENWRT = "25.12.6"
OW_BASE = f"https://downloads.openwrt.org/releases/{OPENWRT}/targets/x86/64/"
DEB_BASE = "https://cloud.debian.org/images/cloud/trixie/latest/"


def fetch_text(url):
    with urllib.request.urlopen(url, timeout=40) as r:
        return r.read().decode("utf-8")


def main():
    prepare_dirs()
    ow_name = f"openwrt-{OPENWRT}-x86-64-generic-ext4-combined.img.gz"
    deb_name = "debian-13-generic-amd64.qcow2"
    ow_sums = fetch_text(OW_BASE + "sha256sums")
    deb_sums = fetch_text(DEB_BASE + "SHA512SUMS")
    def checksum(text, name):
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*").removeprefix("./") == name:
                return parts[0]
        raise ValueError(f"Official checksum missing: {name}")
    jobs = [(OW_BASE, ow_name, checksum(ow_sums, ow_name), "sha256"),
            (DEB_BASE, deb_name, checksum(deb_sums, deb_name), "sha512")]
    (IMAGES / "official-checksums.json").write_text(json.dumps([
        {"url": base + name, "filename": name, "checksum": check, "algorithm": algo}
        for base, name, check, algo in jobs], indent=2) + "\n", encoding="utf-8")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(download, base + name, IMAGES / name, check, algo)
                   for base, name, check, algo in jobs]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()

"""The isolated blast-radius area for a scenario.

A scenario must never risk the test machine, so all writable fault activity is
confined to a *sandbox*: preferably a small dedicated ext4 filesystem on a loop
device (so ``os://disk/usage`` shows a real partition filling up), otherwise a
bounded directory under a workspace path.

The sandbox is created before fault injection and torn down after cleanup; the
teardown is best-effort and idempotent so a crashed run can be recovered.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .capabilities import Capabilities


@dataclass
class Sandbox:
    root: Path
    mount: str
    is_real_fs: bool
    size_mb: int
    _image: Path | None = None

    @property
    def dir(self) -> Path:
        return self.root


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def create(caps: Capabilities, size_mb: int = 64, base: Path | None = None) -> Sandbox:
    base = base or Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)

    if caps.loop_mount:
        try:
            return _create_loop_fs(base, size_mb)
        except (OSError, subprocess.SubprocessError):
            caps.note("loop filesystem creation failed; falling back to bounded directory")

    root = Path(tempfile.mkdtemp(prefix="hostops-sbx-", dir=str(base)))
    return Sandbox(root=root, mount=str(root), is_real_fs=False, size_mb=size_mb)


def _create_loop_fs(base: Path, size_mb: int) -> Sandbox:
    image = base / f"hostops-{os.getpid()}-{size_mb}m.img"
    mount = base / f"hostops-mnt-{os.getpid()}"
    mount.mkdir(parents=True, exist_ok=True)
    _run(["dd", "if=/dev/zero", f"of={image}", "bs=1M", f"count={size_mb}", "status=none"])
    _run(["sudo", "mkfs.ext4", "-q", "-F", str(image)])
    _run(["sudo", "mount", "-o", "loop", str(image), str(mount)])
    _run(["sudo", "chmod", "0777", str(mount)])
    return Sandbox(
        root=mount, mount=str(mount), is_real_fs=True, size_mb=size_mb, _image=image
    )


def destroy(sbx: Sandbox) -> None:
    if sbx.is_real_fs:
        subprocess.run(["sudo", "umount", sbx.mount], capture_output=True)
        if sbx._image and sbx._image.exists():
            sbx._image.unlink(missing_ok=True)
        try:
            Path(sbx.mount).rmdir()
        except OSError:
            pass
    else:
        shutil.rmtree(sbx.root, ignore_errors=True)

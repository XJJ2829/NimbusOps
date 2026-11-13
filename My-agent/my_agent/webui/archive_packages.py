"""Local archive import helpers for WebUI-managed tools and skills."""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from my_agent.agent.skills import BUILTIN_SKILLS_DIR

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_MD_BYTES = 1_000_000
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "metadata",
    "always",
    "license",
    "allowed-tools",
}
ALLOWED_RESOURCE_DIRS = {"scripts", "references", "assets"}
IGNORED_ARCHIVE_NAMES = {".DS_Store", ".localized", "Thumbs.db", "desktop.ini"}
IGNORED_ARCHIVE_DIRS = {
    "__MACOSX",
    "__pycache__",
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "env",
    "venv",
}
IGNORED_ARCHIVE_SUFFIXES = {".pyc", ".pyo"}
ARCHIVE_COPY_IGNORE_PATTERNS = (
    *IGNORED_ARCHIVE_NAMES,
    *IGNORED_ARCHIVE_DIRS,
    "*.pyc",
    "*.pyo",
)
SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


class ArchivePackageError(ValueError):
    """User-facing package import failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ValidatedSkillPackage:
    name: str
    description: str
    source_dir: Path
    raw_markdown: str


def install_skill_archive(data: bytes, filename: str | None = None) -> dict[str, Any]:
    """Validate and install a zip-packaged Skill into bundled ``my_agent/skills``."""
    with tempfile.TemporaryDirectory(prefix="my-agent-skill-") as tmp:
        root = Path(tmp)
        _extract_zip_archive(data, root, filename=filename)
        skill = find_skill_package(root)
        target = (BUILTIN_SKILLS_DIR / skill.name).resolve(strict=False)
        skills_root = BUILTIN_SKILLS_DIR.resolve(strict=False)
        _ensure_inside(target, skills_root)
        if target.exists():
            raise ArchivePackageError(f"Skill '{skill.name}' already exists.")
        _copy_skill_tree(skill.source_dir, target)
        return {
            "ok": True,
            "message": f"Skill '{skill.name}' added successfully.",
            "skill": {
                "name": skill.name,
                "description": skill.description,
                "source": "builtin",
                "available": True,
                "unavailable_reason": "",
            },
        }


def find_skill_package(
    root: Path,
    *,
    expected_name: str | None = None,
    allowed_extra_files: set[str] | None = None,
    allowed_extra_dirs: set[str] | None = None,
) -> ValidatedSkillPackage:
    """Find exactly one ``SKILL.md`` below ``root`` and validate the skill package."""
    skill_files = [
        path
        for path in root.rglob("SKILL.md")
        if "__MACOSX" not in path.parts and path.is_file()
    ]
    if not skill_files:
        raise ArchivePackageError("SKILL.md not found in archive.")
    if len(skill_files) > 1:
        raise ArchivePackageError("Archive contains multiple SKILL.md files.")
    skill_dir = skill_files[0].parent
    return validate_skill_directory(
        skill_dir,
        expected_name=expected_name,
        allowed_extra_files=allowed_extra_files or set(),
        allowed_extra_dirs=allowed_extra_dirs or set(),
    )


def validate_skill_directory(
    skill_dir: Path,
    *,
    expected_name: str | None = None,
    allowed_extra_files: set[str] | None = None,
    allowed_extra_dirs: set[str] | None = None,
) -> ValidatedSkillPackage:
    skill_md = skill_dir / "SKILL.md"
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArchivePackageError(f"Could not read SKILL.md: {exc}") from exc
    if len(raw.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise ArchivePackageError("SKILL.md is too large.")
    match = _STRIP_SKILL_FRONTMATTER.match(raw)
    if not match:
        raise ArchivePackageError("SKILL.md must start with YAML frontmatter.")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ArchivePackageError(f"Invalid YAML in SKILL.md frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ArchivePackageError("SKILL.md frontmatter must be a YAML object.")
    unexpected = sorted(set(str(key) for key in frontmatter) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected:
        raise ArchivePackageError(
            "Unexpected SKILL.md frontmatter key(s): " + ", ".join(unexpected)
        )
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ArchivePackageError("SKILL.md frontmatter must include a string 'name'.")
    name = name.strip()
    if not SKILL_NAME_RE.fullmatch(name) or len(name) > MAX_SKILL_NAME_LENGTH:
        raise ArchivePackageError(
            "Skill name must be lower hyphen-case and at most 64 characters."
        )
    if expected_name and name != expected_name:
        raise ArchivePackageError(f"Skill name must be '{expected_name}' for this package.")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ArchivePackageError("SKILL.md frontmatter must include a non-empty description.")
    description = description.strip()
    lowered_description = description.lower()
    if "[todo" in lowered_description or "todo:" in lowered_description:
        raise ArchivePackageError("Skill description still contains TODO placeholder text.")
    if "<" in description or ">" in description:
        raise ArchivePackageError("Skill description cannot contain angle brackets.")
    if len(description) > 1024:
        raise ArchivePackageError("Skill description is too long.")
    _validate_skill_root_contents(
        skill_dir,
        allowed_extra_files or set(),
        allowed_extra_dirs or set(),
    )
    return ValidatedSkillPackage(
        name=name,
        description=description,
        source_dir=skill_dir,
        raw_markdown=raw,
    )


def copy_validated_skill(skill: ValidatedSkillPackage, target_dir: Path) -> None:
    _copy_skill_tree(skill.source_dir, target_dir)


def _validate_skill_root_contents(
    skill_dir: Path,
    allowed_extra_files: set[str],
    allowed_extra_dirs: set[str],
) -> None:
    for child in skill_dir.iterdir():
        if child.name in IGNORED_ARCHIVE_NAMES:
            continue
        if child.name == "SKILL.md":
            continue
        if child.is_dir() and child.name in ALLOWED_RESOURCE_DIRS:
            continue
        if child.is_dir() and child.name in allowed_extra_dirs:
            continue
        if child.is_file() and child.name in allowed_extra_files:
            continue
        raise ArchivePackageError(
            f"Unexpected file or directory in skill root: {child.name}. "
            "Only SKILL.md, scripts/, references/, and assets/ are allowed."
        )


def _copy_skill_tree(source_dir: Path, target_dir: Path) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target_dir.with_name(f".{target_dir.name}.incoming.{os.getpid()}")
    if temp_target.exists():
        shutil.rmtree(temp_target)
    try:
        shutil.copytree(
            source_dir,
            temp_target,
            ignore=shutil.ignore_patterns(*ARCHIVE_COPY_IGNORE_PATTERNS),
        )
        temp_target.replace(target_dir)
    except Exception:
        if temp_target.exists():
            shutil.rmtree(temp_target, ignore_errors=True)
        raise


def _extract_zip_archive(data: bytes, target_root: Path, *, filename: str | None = None) -> None:
    if not data:
        raise ArchivePackageError("Archive file is empty.")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ArchivePackageError("Archive is too large.")
    if filename and not filename.lower().endswith(".zip"):
        raise ArchivePackageError("Only .zip archives are supported.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchivePackageError("Archive is not a valid zip file.") from exc
    with archive:
        infos = archive.infolist()
        for info in infos:
            _validate_zip_member(info)
        files = [
            info
            for info in infos
            if not info.is_dir() and not _is_ignored_archive_member(info.filename)
        ]
        if len(files) > MAX_ARCHIVE_FILES:
            raise ArchivePackageError("Archive contains too many files.")
        total_size = sum(max(0, info.file_size) for info in files)
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ArchivePackageError("Archive expands to too much data.")
        for info in infos:
            name = info.filename.replace("\\", "/")
            if _is_ignored_archive_member(name):
                continue
            destination = (target_root / name).resolve(strict=False)
            _ensure_inside(destination, target_root.resolve(strict=False))
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename.replace("\\", "/")
    if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
        raise ArchivePackageError(f"Unsafe archive path: {info.filename}")
    if Path(name).is_absolute() or any(part in {"", ".", ".."} for part in Path(name).parts):
        raise ArchivePackageError(f"Unsafe archive path: {info.filename}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise ArchivePackageError("Archive cannot contain symbolic links.")


def _is_ignored_archive_member(filename: str) -> bool:
    path = Path(filename.replace("\\", "/"))
    parts = set(path.parts)
    if parts & IGNORED_ARCHIVE_DIRS:
        return True
    if path.name in IGNORED_ARCHIVE_NAMES:
        return True
    return path.suffix.lower() in IGNORED_ARCHIVE_SUFFIXES


def _ensure_inside(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ArchivePackageError("Archive path escapes the target directory.") from exc

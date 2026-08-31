"""Project instruction baseline loader — the "AGENTS.md" of Second Person.

Concept mirrors deepseek-harness `@deepseek-ai/dsh-agent-instructions`: read a
small ordered set of workspace-level instruction files from the project root
into the first request, so the model knows the project's identity without the
user having to explain it. On later turns, the baseline is already in `history`
and reused from the provider prefix cache; only a hash-detected change adds a
short "changes" batch to the tail.

Loading here is intentionally read-only and side-effect-free — the callers
persist the reconciliation state and emit context events; this module just
returns a payload.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("second_person.project_instructions")

# Broad-to-specific candidate order. The first file kept in this order is
# considered the most "authoritative" identity of the project; subsequent
# files add detail. Everything is optional — a project with none of these
# contributes an empty baseline (which is a valid state, and the callers
# suppress the context block rather than emitting an empty one).
DEFAULT_CANDIDATES: tuple[str, ...] = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS.local.md",
    "CLAUDE.local.md",
    "docs/README.md",
    "docs/index.md",
    "docs/OVERVIEW.md",
)

# Total UTF-8 byte budget for the full baseline payload. Sized to fit the
# common "project overview" set comfortably while staying small enough that
# the first-turn miss it costs is a one-time expense — every subsequent turn
# reuses it from history via the provider prefix cache.
DEFAULT_MAX_BYTES = 32_768

# Per-file cap before truncation kicks in. Prevents one enormous README from
# swallowing the whole budget and hiding every other file entirely.
DEFAULT_MAX_SOURCE_BYTES = 16_384

TRUNCATION_MARKER = "\n\n… (truncated to fit the workspace instruction budget)"

BASELINE_INTRO = (
    "The following workspace instructions describe the current project. "
    "Use them as authoritative context when the user references "
    "\"this project\", \"the codebase\", or similar. "
    "More specific files take precedence over broader ones. "
    "They do not override system rules or direct user instructions."
)

CHANGES_INTRO = (
    "The workspace instructions above have changed since they were loaded. "
    "Use the following updated content instead of the previously loaded "
    "content for these files."
)

EMPTY_BASELINE_MARKER = "(no workspace instructions found for this project)"


@dataclass(frozen=True)
class InstructionFile:
    """One loaded workspace instruction file."""
    path: str          # display path, relative to project root, forward slashes
    content: str       # already truncated to per-file cap
    hash: str          # sha256 of the raw (untruncated) file bytes


@dataclass(frozen=True)
class BaselineResult:
    """One reconciliation outcome for a session's project baseline."""
    # kind ∈ {"initial", "changes", "unchanged", "empty"}
    #   initial:   first time we see this session — emit full baseline block
    #   changes:   at least one file changed since last state — emit changes block
    #   unchanged: same files_hash → do not emit any block (prefix cache wins)
    #   empty:     project has no candidate files — do not emit any block
    kind: str
    files: tuple[InstructionFile, ...] = field(default_factory=tuple)
    changed: tuple[InstructionFile, ...] = field(default_factory=tuple)
    removed: tuple[str, ...] = field(default_factory=tuple)  # display paths only
    files_hash: str = ""     # aggregate hash of `files`; empty for "empty" kind
    total_bytes: int = 0
    truncated: bool = False

    def render_full(self) -> str:
        """Render the initial baseline block for a first-time injection."""
        if not self.files:
            return ""
        sections = [BASELINE_INTRO]
        for f in self.files:
            sections.append(_format_section(f.path, f.content))
        return "\n\n".join(sections)

    def render_changes(self) -> str:
        """Render only the delta since the last injection."""
        if not self.changed and not self.removed:
            return ""
        sections = [CHANGES_INTRO]
        for f in self.changed:
            sections.append(_format_section(f.path, f.content, updated=True))
        for path in self.removed:
            sections.append(
                f"### {path} (removed)\n\nThe previously loaded instructions "
                f"from `{path}` no longer apply.")
        return "\n\n".join(sections)


def _format_section(path: str, content: str, *, updated: bool = False) -> str:
    header = f"### {path}" + (" (updated)" if updated else "")
    return f"{header}\n\n{content}"


def _read_candidate(root: Path, rel: str, max_source_bytes: int
                    ) -> InstructionFile | None:
    """Read one candidate file safely; None if missing / unreadable / empty."""
    try:
        p = (root / rel).resolve()
        # Refuse to follow anything that escapes the project root — a symlink
        # into $HOME could leak private notes into a shared prompt.
        try:
            p.relative_to(root.resolve())
        except ValueError:
            logger.debug("拒绝项目根外的说明书候选：%s", rel)
            return None
        if not p.is_file():
            return None
        raw_bytes = p.read_bytes()
    except OSError:
        return None
    if not raw_bytes.strip():
        return None
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if len(text.encode("utf-8")) > max_source_bytes:
        # Byte-aware truncation: cut at a UTF-8 boundary and add a marker.
        encoded = text.encode("utf-8")[:max_source_bytes]
        # Backtrack to a valid UTF-8 boundary if we split mid-codepoint.
        while encoded and (encoded[-1] & 0xC0) == 0x80:
            encoded = encoded[:-1]
        text = encoded.decode("utf-8", errors="ignore") + TRUNCATION_MARKER
    # Normalize display path with forward slashes for cross-platform stability.
    display_path = rel.replace("\\", "/")
    return InstructionFile(path=display_path, content=text, hash=file_hash)


def load_baseline(project_root: Path,
                  candidates: tuple[str, ...] = DEFAULT_CANDIDATES,
                  max_bytes: int = DEFAULT_MAX_BYTES,
                  max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
                  ) -> tuple[tuple[InstructionFile, ...], bool]:
    """Load candidate files under `project_root`, respecting byte budgets.

    Returns (files, truncated). Files preserve the broad-to-specific candidate
    order. Duplicate content (a CLAUDE.md byte-identical to an already-loaded
    AGENTS.md) is dropped so the model does not see the same prose twice.
    """
    if not project_root or not project_root.exists():
        return (), False
    root = Path(project_root)
    loaded: list[InstructionFile] = []
    seen_hashes: set[str] = set()
    remaining = max_bytes
    truncated = False
    for rel in candidates:
        file = _read_candidate(root, rel, max_source_bytes)
        if file is None:
            continue
        if file.hash in seen_hashes:
            continue
        rendered = _format_section(file.path, file.content)
        rendered_bytes = len(rendered.encode("utf-8"))
        if rendered_bytes > remaining:
            # Not enough budget left even after per-file truncation — stop
            # here rather than emit half a file. Broader files already took
            # priority; the omitted specifics can be pulled on demand via
            # fs_read when the model needs them.
            truncated = True
            break
        loaded.append(file)
        seen_hashes.add(file.hash)
        remaining -= rendered_bytes
    return tuple(loaded), truncated


def aggregate_hash(files: tuple[InstructionFile, ...]) -> str:
    """Stable hash across the ordered file list. Same input → same output."""
    if not files:
        return ""
    h = hashlib.sha256()
    for f in files:
        h.update(f.path.encode("utf-8"))
        h.update(b"\0")
        h.update(f.hash.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def reconcile(previous_files_hash: str | None,
              previous_paths: dict[str, str] | None,
              current_files: tuple[InstructionFile, ...],
              truncated: bool) -> BaselineResult:
    """Compare newly loaded files with the previously injected snapshot.

    `previous_paths` maps display_path → per-file hash for what was previously
    injected. `None` means "no prior state" — treat as initial.
    """
    current_hash = aggregate_hash(current_files)
    if not current_files:
        # Empty is empty regardless of prior state: any prior baseline is left
        # in history as-is. Emitting a "removed" block for every file each
        # turn would defeat the whole cache stability point.
        return BaselineResult(kind="empty")
    if previous_files_hash is None:
        return BaselineResult(kind="initial", files=current_files,
                              files_hash=current_hash,
                              total_bytes=_total_bytes(current_files),
                              truncated=truncated)
    if current_hash == previous_files_hash:
        return BaselineResult(kind="unchanged", files=current_files,
                              files_hash=current_hash,
                              total_bytes=_total_bytes(current_files),
                              truncated=truncated)
    prev = previous_paths or {}
    changed: list[InstructionFile] = []
    for f in current_files:
        if prev.get(f.path) != f.hash:
            changed.append(f)
    current_paths = {f.path for f in current_files}
    removed = tuple(sorted(p for p in prev if p not in current_paths))
    return BaselineResult(
        kind="changes", files=current_files,
        changed=tuple(changed), removed=removed,
        files_hash=current_hash,
        total_bytes=_total_bytes(current_files), truncated=truncated)


def _total_bytes(files: tuple[InstructionFile, ...]) -> int:
    return sum(len(f.content.encode("utf-8")) for f in files)


def paths_hash_map(files: tuple[InstructionFile, ...]) -> dict[str, str]:
    """Materialize the {display_path: file_hash} map for persistence."""
    return {f.path: f.hash for f in files}

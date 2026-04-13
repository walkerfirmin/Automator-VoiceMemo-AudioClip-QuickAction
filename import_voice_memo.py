#!/usr/bin/env python3
"""
Import an external .m4a into Apple Voice Memos so it can appear and play in the app.

Dependencies:
  - Python 3 (stdlib only)
  - FFmpeg in PATH: ffprobe (required); ffmpeg (required for --re-encode)
  - macOS: Voice Memos data under the Group Container path below
  - System Settings → Privacy & Security → Full Disk Access for your terminal
    if macOS blocks reads/writes under ~/Library/Group Containers/

Usage:
  1. Quit Voice Memos completely.
  2. Run: python3 import_voice_memo.py /path/to/test.m4a --label "My import"
  3. Open Voice Memos and confirm playback.

See --help for flags (--dry-run, --re-encode, --no-db, --use-mtime, --list-libraries,
--clean-missing-audio).

This modifies CloudRecordings.db (Core Data SQLite). A timestamped backup is created
beside the database before any write. Transcription after import is not guaranteed.

If the import \"succeeds\" but nothing appears in Voice Memos, you are probably writing
to a different library than the app uses: run --list-libraries and pass
--recordings-dir to the folder that matches your real memo count. iCloud can also
overwrite local DB changes when the app opens; try quitting, importing, then opening
the app once, or temporarily disabling Voice Memos in iCloud to test.

Automator / Finder Quick Actions:
  - Pass one or more file paths as arguments (Finder often passes multiple files).
  - Use --label-from-filename so each memo title matches the file name (no extension).
  - The workflow runner needs Full Disk Access (e.g. add \"Automator\", \"Shortcuts\",
    or \"runWorkflow\" under System Settings → Privacy & Security → Full Disk Access),
    not only Terminal.
  - If ffprobe is not found, set PATH in the shell step, e.g.
    export PATH=\"/opt/homebrew/bin:/usr/local/bin:$PATH\"
  - With --re-encode, ffmpeg writes to the system temp dir first (not the Voice Memos folder)
    so Automator is not blocked by TCC on the ffmpeg binary.
  - Voice Memos must be quit before import; a Quick Action cannot do that for the user.
    Consider a first step \"Quit Application\" → Voice Memos, or accept a failure if it
    is still open.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()

# macOS Sonoma+ (group container). Some setups still use the app container or legacy path.
VOICE_MEMOS_GROUP_RECORDINGS = HOME / (
    "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
)
VOICE_MEMOS_APP_CONTAINER_RECORDINGS = HOME / (
    "Library/Containers/com.apple.VoiceMemos/Data/Library/Application Support/Recordings"
)
VOICE_MEMOS_LEGACY_RECORDINGS = HOME / (
    "Library/Application Support/com.apple.voicememos/Recordings"
)
VOICE_MEMOS_LEGACY_BASE = HOME / "Library/Application Support/com.apple.voicememos"

# Default when no explicit --recordings-dir (resolved by pick_active_recordings_dir).
VOICE_MEMOS_RECORDINGS = VOICE_MEMOS_GROUP_RECORDINGS

RECORDINGS_DIR_CANDIDATES: tuple[Path, ...] = (
    VOICE_MEMOS_GROUP_RECORDINGS,
    VOICE_MEMOS_APP_CONTAINER_RECORDINGS,
    VOICE_MEMOS_LEGACY_RECORDINGS,
    VOICE_MEMOS_LEGACY_BASE,
)

CLOUD_DB_NAME = "CloudRecordings.db"

CORE_DATA_EPOCH_UNIX = 978307200.0  # 2001-01-01 00:00:00 UTC

# Avoid matching arbitrary *IDENTIFIER* columns; stick to UUID-ish names.
_UUID_TOKEN = re.compile(r"(^|_)UUID($|_)", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Copy an .m4a into Voice Memos and register it in CloudRecordings.db.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "inputs",
        type=Path,
        nargs="*",
        metavar="FILE",
        help="One or more source audio files (e.g. .m4a). Omit with --list-libraries "
        "or --clean-missing-audio. Multiple paths suit Automator/Finder Quick Actions.",
    )
    p.add_argument(
        "--label",
        default="Imported recording",
        help="Title stored in ZCUSTOMLABEL / ZENCRYPTEDTITLE (default: %(default)s). "
        "Ignored for a file when --label-from-filename is set.",
    )
    p.add_argument(
        "--label-from-filename",
        action="store_true",
        help="Use each input file's basename (without extension) as the memo title.",
    )
    p.add_argument(
        "--re-encode",
        action="store_true",
        help="Re-encode with FFmpeg (aac_at on macOS when available, else aac) "
        "and -movflags +faststart for better Voice Memos compatibility.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only; do not copy files or modify the database.",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Only place the .m4a in Recordings; do not insert a database row.",
    )
    p.add_argument(
        "--use-mtime",
        action="store_true",
        help="Set ZDATE from the source file mtime instead of current time.",
    )
    p.add_argument(
        "--recordings-dir",
        type=Path,
        default=None,
        help="Use this Recordings folder (contains CloudRecordings.db). "
        "If omitted, the script picks the candidate with the most ZCLOUDRECORDING rows.",
    )
    p.add_argument(
        "--list-libraries",
        action="store_true",
        help="List known CloudRecordings.db locations, row counts, and exit (no import).",
    )
    p.add_argument(
        "--clean-missing-audio",
        action="store_true",
        help="Remove ZCLOUDRECORDING rows whose ZPATH file is missing from the library "
        "folder (or ZPATH is empty). Use with --dry-run to list only. Does not require "
        "input .m4a.",
    )
    return p.parse_args()


PERMISSIONS_HINT = (
    "macOS blocked access to the Voice Memos folder. Grant Full Disk Access to this "
    "terminal app: System Settings → Privacy & Security → Full Disk Access → add "
    "Terminal (or iTerm, Cursor, etc.), then restart the terminal."
)


def _recordings_dir_priority(p: Path) -> int:
    """Lower is preferred when row counts (and db mtime) tie."""
    try:
        r = p.resolve()
    except OSError:
        return 99
    if r == VOICE_MEMOS_GROUP_RECORDINGS.resolve():
        return 0
    if r == VOICE_MEMOS_APP_CONTAINER_RECORDINGS.resolve():
        return 1
    if r == VOICE_MEMOS_LEGACY_RECORDINGS.resolve():
        return 2
    if r == VOICE_MEMOS_LEGACY_BASE.resolve():
        return 3
    return 10


def zcloudrecording_count(db_path: Path) -> int | None:
    try:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM ZCLOUDRECORDING").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def scan_recordings_libraries() -> list[tuple[Path, Path, int, float]]:
    """Return tuples: (recordings_dir, db_path, row_count, db_mtime)."""
    out: list[tuple[Path, Path, int, float]] = []
    for rec in RECORDINGS_DIR_CANDIDATES:
        dbp = rec / CLOUD_DB_NAME
        if not rec.is_dir() or not dbp.is_file():
            continue
        n = zcloudrecording_count(dbp)
        if n is None:
            continue
        try:
            mtime = dbp.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append((rec.resolve(), dbp.resolve(), n, mtime))
    return out


def pick_active_recordings_dir() -> Path | None:
    """Choose the library Voice Memos is most likely using (most rows, then newest db)."""
    rows = scan_recordings_libraries()
    if not rows:
        return None
    best = max(
        rows,
        key=lambda t: (t[2], t[3], -_recordings_dir_priority(t[0])),
    )
    return best[0]


def print_library_scan() -> None:
    found = scan_recordings_libraries()
    print("Voice Memos CloudRecordings.db candidates:\n", flush=True)
    if not found:
        print(
            "  (none found — open Voice Memos once, or check Full Disk Access.)",
            flush=True,
        )
        return
    for rec, dbp, n, mtime in sorted(found, key=lambda t: (-t[2], -t[3], str(t[0]))):
        age = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  rows={n:5d}  db_mtime={age}", flush=True)
        print(f"    {rec}", flush=True)
    chosen = pick_active_recordings_dir()
    if chosen:
        print(f"\nDefault import target (--recordings-dir omitted): {chosen}", flush=True)


def register_core_data_sqlite_stub_functions(conn: sqlite3.Connection) -> None:
    """Register no-op stand-ins for Core Data SQL functions used in DB triggers.

    Voice Memos' CloudRecordings.db defines AFTER INSERT/UPDATE/DELETE triggers that
    call NSCoreDataDATrigger* helpers. Those exist only when SQLite is driven by
    Apple's Core Data stack; CPython's sqlite3 does not provide them. Without stubs,
    INSERT INTO ZCLOUDRECORDING fails with \"no such function\".
    """

    def _noop(*_args: object) -> None:
        return None

    # num_params=-1 allows any arity (trigger SQL passes several arguments).
    conn.create_function(
        "NSCoreDataDATriggerUpdatedAffectedObjectValue",
        -1,
        _noop,
    )
    conn.create_function(
        "NSCoreDataDATriggerInsertUpdatedAffectedObjectValue",
        -1,
        _noop,
    )


def check_recordings_writable(recordings: Path) -> None:
    """Fail fast with a clear message if TCC denies writes to the Group Container."""
    probe = recordings / ".import_voice_memo_write_probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        if isinstance(e, PermissionError) or e.errno == 1:
            raise SystemExit(f"{PERMISSIONS_HINT}\nUnderlying error: {e}") from e
        raise


def require_voice_memos_quit() -> None:
    try:
        r = subprocess.run(
            ["pgrep", "-x", "VoiceMemos"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"Could not check for Voice Memos process: {e}") from e
    if r.returncode == 0:
        raise SystemExit(
            "Voice Memos is running. Quit the app completely, then run this script again."
        )


def run_cmd(
    argv: list[str], *, dry_run: bool, capture: bool = True
) -> subprocess.CompletedProcess:
    if dry_run:
        print(f"  [dry-run] would run: {' '.join(argv)}")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    kw: dict = {"timeout": 600}
    if capture:
        kw["capture_output"] = True
        kw["text"] = True
    return subprocess.run(argv, check=False, **kw)


def ffprobe_duration(path: Path, *, dry_run: bool) -> float:
    if dry_run:
        print(f"  [dry-run] would run ffprobe on {path}")
        return 1.0
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    cp = run_cmd(argv, dry_run=False)
    if cp.returncode != 0:
        raise SystemExit(
            f"ffprobe failed (exit {cp.returncode}). Is FFmpeg installed? stderr: {cp.stderr}"
        )
    s = (cp.stdout or "").strip()
    if not s:
        raise SystemExit("ffprobe returned empty duration.")
    try:
        return float(s)
    except ValueError as e:
        raise SystemExit(f"Could not parse duration from ffprobe: {s!r}") from e


def ffmpeg_has_aac_at() -> bool:
    try:
        cp = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0 and "aac_at" in (cp.stdout or "")


def reencode_m4a(
    src: Path, dst: Path, *, dry_run: bool
) -> None:
    encoder = "aac_at" if ffmpeg_has_aac_at() else "aac"
    argv = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c:a",
        encoder,
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    if dry_run:
        run_cmd(argv, dry_run=True)
        return
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if cp.returncode != 0:
        raise SystemExit(
            f"ffmpeg re-encode failed (exit {cp.returncode}). stderr:\n{cp.stderr}"
        )


def voice_memos_basename(when: datetime | None = None, suffix8: str | None = None) -> str:
    """Pattern: YYYYMMDD HHMMSS-xxxxxxxx.m4a (local time, space before time)."""
    if when is None:
        when = datetime.now().astimezone()
    if suffix8 is None:
        suffix8 = uuid.uuid4().hex[:8].upper()
    date_part = when.strftime("%Y%m%d %H%M%S")
    return f"{date_part}-{suffix8}.m4a"


def unix_to_core_data(ts: float) -> float:
    return ts - CORE_DATA_EPOCH_UNIX


def format_uuid_like(template: object | None, new: uuid.UUID) -> str | bytes | None:
    """Match template's style (hex only, dashed, braces) when possible."""
    if template is None:
        return None
    if isinstance(template, bytes):
        try:
            t = template.decode("utf-8", errors="replace")
        except Exception:
            return template
    elif isinstance(template, str):
        t = template
    else:
        return str(new)

    h = new.hex
    u = str(new)
    if len(t) == 32 and all(c in "0123456789abcdefABCDEF" for c in t):
        return h
    if len(t) == 36 and t.count("-") == 4:
        return u
    if t.startswith("{") and t.endswith("}"):
        return "{" + u + "}"
    return u


def is_uuid_column(name: str) -> bool:
    if name in ("Z_PK", "Z_ENT", "Z_OPT"):
        return False
    u = name.upper()
    if "UNIQUEID" in u or "UNIQUE_ID" in u:
        return True
    if _UUID_TOKEN.search(name):
        return True
    if u == "ZGUID" or u.endswith("_GUID"):
        return True
    return False


def backup_db(db_path: Path, *, dry_run: bool) -> Path | None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.backup.{stamp}")
    if dry_run:
        print(f"  [dry-run] would copy {db_path} -> {backup}")
        return backup
    shutil.copy2(db_path, backup)
    for ext in ("-wal", "-shm"):
        side = Path(str(db_path) + ext)
        if side.is_file():
            shutil.copy2(side, Path(str(backup) + ext))
    print(f"Backed up database to {backup}")
    return backup


def load_table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # cid, name, type, notnull, dflt_value, pk
    return [
        {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": r[3],
            "default": r[4],
            "pk": r[5],
        }
        for r in rows
    ]


def clone_latest_recording_row(
    conn: sqlite3.Connection,
) -> tuple[dict[str, object | None], list[str]]:
    colinfo = load_table_columns(conn, "ZCLOUDRECORDING")
    names = [c["name"] for c in colinfo]
    row = conn.execute(
        "SELECT * FROM ZCLOUDRECORDING ORDER BY Z_PK DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise SystemExit(
            "No existing rows in ZCLOUDRECORDING. Create at least one memo in Voice Memos "
            "so this script can clone schema-compatible values."
        )
    return dict(zip(names, row)), names


def apply_new_recording_fields(
    data: dict[str, object | None],
    *,
    new_pk: int,
    zpath: str,
    duration: float,
    zdate_core: float,
    label: str,
) -> dict[str, object | None]:
    out = dict(data)
    out["Z_PK"] = new_pk
    out["ZPATH"] = zpath
    out["ZDURATION"] = duration
    # UI list duration uses ZLOCALDURATION for on-disk memos; cloning left the template's
    # value (e.g. always ~1:37). Keep both in sync with ffprobe-measured seconds.
    if "ZLOCALDURATION" in out:
        out["ZLOCALDURATION"] = duration
    out["ZDATE"] = zdate_core
    out["Z_OPT"] = 1

    if "ZCUSTOMLABEL" in out:
        out["ZCUSTOMLABEL"] = label
    if "ZENCRYPTEDTITLE" in out:
        out["ZENCRYPTEDTITLE"] = label

    new_u = uuid.uuid4()
    for key in list(out.keys()):
        if is_uuid_column(key):
            out[key] = format_uuid_like(data.get(key), new_u)

    # Core Data defines AFTER INSERT triggers on ZCLOUDRECORDING that run only when
    # ZFOLDER IS NOT NULL; they call NSCoreDataDATriggerUpdatedAffectedObjectValue,
    # which exists only inside Apple's frameworks — not in Python's sqlite3. New rows
    # must use ZFOLDER NULL (root / unfiled) so those triggers do not fire.
    if "ZFOLDER" in out:
        out["ZFOLDER"] = None

    return out


def insert_row(
    conn: sqlite3.Connection,
    columns: list[str],
    data: dict[str, object | None],
) -> None:
    placeholders = ", ".join(["?"] * len(columns))
    cols_sql = ", ".join(columns)
    values = [data.get(c) for c in columns]
    conn.execute(f"INSERT INTO ZCLOUDRECORDING ({cols_sql}) VALUES ({placeholders})", values)


def safe_audio_path_in_library(recordings: Path, zpath: object) -> Path | None:
    """Resolve ZPATH to a file path inside recordings, or None if empty or unsafe.

    Voice Memos normally stores a basename only. Paths with \"..\" or absolute
    segments are rejected so we never touch files outside the library folder.
    """
    if zpath is None or not isinstance(zpath, str):
        return None
    raw = zpath.strip()
    if not raw:
        return None
    if raw.startswith("/"):
        return None
    parts = Path(raw).parts
    if ".." in parts or (parts and parts[0] == ".."):
        return None
    rec = recordings.resolve()
    candidate = (recordings / raw).resolve()
    try:
        candidate.relative_to(rec)
    except ValueError:
        return None
    return candidate


def row_is_orphan_missing_file(recordings: Path, zpath: object) -> bool:
    """True if ZPATH is empty or the referenced file is not present under recordings."""
    if zpath is None or not isinstance(zpath, str) or not zpath.strip():
        return True
    resolved = safe_audio_path_in_library(recordings, zpath)
    if resolved is None:
        return False
    return not resolved.is_file()


def row_has_unsafe_zpath(recordings: Path, zpath: object) -> bool:
    """True if ZPATH is non-empty but cannot be resolved safely under recordings."""
    if zpath is None or not isinstance(zpath, str) or not zpath.strip():
        return False
    return safe_audio_path_in_library(recordings, zpath) is None


def delete_zcloudrecording_rows(conn: sqlite3.Connection, z_pks: list[int]) -> int:
    """Delete rows by primary key in chunks. Returns number deleted."""
    if not z_pks:
        return 0
    deleted = 0
    chunk = 500
    for i in range(0, len(z_pks), chunk):
        batch = z_pks[i : i + chunk]
        placeholders = ",".join(["?"] * len(batch))
        cur = conn.execute(
            f"DELETE FROM ZCLOUDRECORDING WHERE Z_PK IN ({placeholders})",
            batch,
        )
        deleted += cur.rowcount if cur.rowcount is not None else len(batch)
    return deleted


def run_clean_missing_audio(args: argparse.Namespace) -> None:
    recordings = (
        args.recordings_dir.expanduser().resolve()
        if args.recordings_dir
        else pick_active_recordings_dir()
    )
    if recordings is None:
        raise SystemExit(
            "Could not find CloudRecordings.db. Run --list-libraries or pass "
            "--recordings-dir."
        )
    db_path = recordings / CLOUD_DB_NAME

    if not args.dry_run:
        require_voice_memos_quit()

    if not recordings.is_dir():
        raise SystemExit(f"Recordings directory not found:\n  {recordings}")
    if not args.dry_run:
        check_recordings_writable(recordings)
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    if not args.recordings_dir:
        n = zcloudrecording_count(db_path)
        extra = f", {n} memo(s) in DB" if n is not None else ""
        print(
            f"Using auto-selected library{extra}:\n  {recordings}",
            flush=True,
        )
    else:
        print(f"Using library from --recordings-dir:\n  {recordings}", flush=True)

    try:
        conn = sqlite3.connect(str(db_path))
    except OSError as e:
        if isinstance(e, PermissionError) or getattr(e, "errno", None) == 1:
            raise SystemExit(f"{PERMISSIONS_HINT}\nUnderlying error: {e}") from e
        raise
    register_core_data_sqlite_stub_functions(conn)
    try:
        rows = conn.execute(
            "SELECT Z_PK, ZPATH, ZCUSTOMLABEL FROM ZCLOUDRECORDING ORDER BY Z_PK"
        ).fetchall()
        orphan_pks: list[int] = []
        orphan_lines: list[tuple[int, str | None, str | None]] = []
        unsafe: list[tuple[int, str]] = []

        for z_pk, zpath, label in rows:
            if row_has_unsafe_zpath(recordings, zpath):
                zp = zpath if isinstance(zpath, str) else ""
                unsafe.append((int(z_pk), zp))
                continue
            if row_is_orphan_missing_file(recordings, zpath):
                zpi = int(z_pk)
                orphan_pks.append(zpi)
                disp = zpath if isinstance(zpath, str) else None
                lab = (label or "")[:80] if isinstance(label, str) else None
                orphan_lines.append((zpi, disp, lab))

        print(
            f"Rows in ZCLOUDRECORDING: {len(rows)}; "
            f"missing-file orphans: {len(orphan_pks)}; "
            f"skipped (unsafe ZPATH): {len(unsafe)}",
            flush=True,
        )
        for z_pk, zpath, lab in orphan_lines:
            print(
                f"  delete Z_PK={z_pk}  ZPATH={zpath!r}  label={lab!r}",
                flush=True,
            )
        for z_pk, zp in unsafe:
            print(
                f"  skip Z_PK={z_pk}  unsafe ZPATH={zp!r}",
                file=sys.stderr,
                flush=True,
            )

        if args.dry_run:
            print(
                "\nDry run: no rows deleted. Run again without --dry-run to remove.",
                flush=True,
            )
            return

        if not orphan_pks:
            print("Nothing to delete.", flush=True)
            return

        backup_db(db_path, dry_run=False)
        conn.execute("BEGIN IMMEDIATE")
        try:
            n_del = delete_zcloudrecording_rows(conn, orphan_pks)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.OperationalError as e:
            print(f"Note: WAL checkpoint skipped ({e}).", file=sys.stderr)

        print(f"Deleted {n_del} row(s) from ZCLOUDRECORDING.", flush=True)
    finally:
        conn.close()


def update_primary_key_max(conn: sqlite3.Connection, z_ent: int, new_pk: int) -> None:
    cur = conn.execute(
        "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_ENT = ?", (z_ent,)
    ).fetchone()
    if not cur:
        print(
            f"Warning: no Z_PRIMARYKEY row for Z_ENT={z_ent}; skipping Z_MAX update.",
            file=sys.stderr,
        )
        return
    current = cur[0]
    try:
        current_int = int(current) if current is not None else 0
    except (TypeError, ValueError):
        current_int = 0
    new_max = max(current_int, int(new_pk))
    conn.execute(
        "UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_ENT = ?",
        (new_max, z_ent),
    )


def run_import_path(
    src: Path,
    recordings: Path,
    db_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
) -> None:
    """Import a single file into Voice Memos (copy/re-encode + DB row). Caller backs up DB."""
    # Filename / time
    if args.use_mtime:
        mtime = src.stat().st_mtime
        when = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone()
        zdate_core = unix_to_core_data(mtime)
    else:
        when = None
        zdate_core = unix_to_core_data(time.time())

    suffix8 = uuid.uuid4().hex[:8].upper()
    basename = voice_memos_basename(when=when, suffix8=suffix8)
    dest_audio = recordings / basename

    print(f"Source:      {src}", flush=True)
    print(f"Destination: {dest_audio}", flush=True)
    print(
        f"Core Data ZDATE (seconds since 2001-01-01 UTC): {zdate_core:.3f}",
        flush=True,
    )

    duration = ffprobe_duration(src, dry_run=args.dry_run)
    print(f"Duration (s): {duration:.3f}", flush=True)

    if args.dry_run:
        print("\nDry run: no files or database will be modified.")

    if args.re_encode:
        # FFmpeg writes the temp file. macOS often denies that binary writes under the
        # Voice Memos Group Container even when Python has Full Disk Access (e.g. Automator).
        # Encode under $TMPDIR, then shutil.move into Recordings (Python only).
        if args.dry_run:
            preview_tmp = Path(tempfile.gettempdir()) / "import_voice_memo.reencode.tmp.m4a"
            reencode_m4a(src, preview_tmp, dry_run=True)
        else:
            fd, tmp_name = tempfile.mkstemp(
                suffix=".m4a", prefix="import_voice_memo_reencode_"
            )
            os.close(fd)
            tmp_out = Path(tmp_name)
            try:
                reencode_m4a(src, tmp_out, dry_run=False)
                dur_source = ffprobe_duration(tmp_out, dry_run=False)
                duration = dur_source
                try:
                    shutil.move(str(tmp_out), str(dest_audio))
                except OSError as e:
                    if isinstance(e, PermissionError) or getattr(e, "errno", None) == 1:
                        raise SystemExit(
                            f"{PERMISSIONS_HINT}\nUnderlying error: {e}"
                        ) from e
                    raise
            finally:
                tmp_out.unlink(missing_ok=True)
    else:
        if not args.dry_run:
            try:
                shutil.copy2(src, dest_audio)
            except OSError as e:
                if isinstance(e, PermissionError) or getattr(e, "errno", None) == 1:
                    raise SystemExit(f"{PERMISSIONS_HINT}\nUnderlying error: {e}") from e
                raise
        else:
            print(f"  [dry-run] would copy {src} -> {dest_audio}")

    if args.no_db:
        print("Done (--no-db: file only, no database row).", flush=True)
        return

    if args.dry_run:
        print(f"\nWould open DB: {db_path}")
        if db_path.is_file():
            print(
                "Would clone latest ZCLOUDRECORDING row, assign new Z_PK / ZPATH / "
                "duration / title / UUID fields."
            )
            print("Would update Z_PRIMARYKEY.Z_MAX for Z_ENT from template row.")
        return

    try:
        conn = sqlite3.connect(str(db_path))
    except OSError as e:
        if isinstance(e, PermissionError) or getattr(e, "errno", None) == 1:
            raise SystemExit(f"{PERMISSIONS_HINT}\nUnderlying error: {e}") from e
        raise
    register_core_data_sqlite_stub_functions(conn)
    new_pk = -1
    try:
        template, col_names = clone_latest_recording_row(conn)
        max_row = conn.execute(
            "SELECT COALESCE(MAX(Z_PK), 0) FROM ZCLOUDRECORDING"
        ).fetchone()
        new_pk = int(max_row[0]) + 1
        z_ent = template.get("Z_ENT")
        if z_ent is None:
            raise SystemExit("Template row has no Z_ENT; cannot update Z_PRIMARYKEY.")

        new_data = apply_new_recording_fields(
            template,
            new_pk=new_pk,
            zpath=basename,
            duration=float(duration),
            zdate_core=float(zdate_core),
            label=label,
        )

        conn.execute("BEGIN IMMEDIATE")
        try:
            insert_row(conn, col_names, new_data)
            update_primary_key_max(conn, int(z_ent), new_pk)
            conn.commit()
        except Exception:
            conn.rollback()
            if dest_audio.is_file():
                try:
                    dest_audio.unlink()
                except OSError:
                    pass
                print(
                    f"Removed audio file after failed DB write: {dest_audio}",
                    file=sys.stderr,
                    flush=True,
                )
            raise
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.OperationalError as e:
            print(f"Note: WAL checkpoint skipped ({e}).", file=sys.stderr)
    finally:
        conn.close()

    print(f"Inserted ZCLOUDRECORDING Z_PK={new_pk} ZPATH={basename!r}", flush=True)
    print(
        "Open Voice Memos and test playback. If it fails, try again with --re-encode.",
        flush=True,
    )


def main() -> None:
    args = parse_args()

    if args.list_libraries:
        print_library_scan()
        return

    if args.clean_missing_audio:
        run_clean_missing_audio(args)
        return

    if not args.inputs:
        raise SystemExit(
            "Missing input file(s). Example:\n"
            "  python3 import_voice_memo.py /path/to/file.m4a --label \"Title\"\n"
            "  python3 import_voice_memo.py *.m4a --label-from-filename\n"
            "To see which library Voice Memos might be using:\n"
            "  python3 import_voice_memo.py --list-libraries\n"
            "To remove DB rows with no audio file:\n"
            "  python3 import_voice_memo.py --clean-missing-audio --dry-run"
        )

    recordings = (
        args.recordings_dir.expanduser().resolve()
        if args.recordings_dir
        else pick_active_recordings_dir()
    )
    if recordings is None:
        raise SystemExit(
            "Could not find CloudRecordings.db in any known folder.\n"
            "Open Voice Memos at least once, then run:\n"
            "  python3 import_voice_memo.py --list-libraries\n"
            "If you see a library with your memos, import with:\n"
            "  python3 import_voice_memo.py FILE.m4a --recordings-dir \"PASTE_PATH_HERE\""
        )
    db_path = recordings / CLOUD_DB_NAME

    if not args.dry_run:
        require_voice_memos_quit()

    if not recordings.is_dir():
        raise SystemExit(
            f"Recordings directory not found:\n  {recordings}\n"
            "Open Voice Memos once, or pass --recordings-dir if your library lives elsewhere."
        )
    if not args.dry_run:
        check_recordings_writable(recordings)
    if not db_path.is_file() and not args.no_db:
        if args.dry_run:
            print(f"Note: database not found (dry-run): {db_path}", file=sys.stderr)
        else:
            raise SystemExit(f"Database not found: {db_path}")

    if not args.recordings_dir:
        n = zcloudrecording_count(db_path) if db_path.is_file() else None
        extra = f", {n} memo(s) in DB" if n is not None else ""
        print(
            f"Using auto-selected library{extra}:\n  {recordings}",
            flush=True,
        )
    else:
        print(f"Using library from --recordings-dir:\n  {recordings}", flush=True)

    n_in = len(args.inputs)
    if not args.dry_run and not args.no_db:
        try:
            backup_db(db_path, dry_run=False)
        except OSError as e:
            if isinstance(e, PermissionError) or getattr(e, "errno", None) == 1:
                raise SystemExit(f"{PERMISSIONS_HINT}\nUnderlying error: {e}") from e
            raise

    for idx, src_raw in enumerate(args.inputs, start=1):
        src = src_raw.expanduser().resolve()
        if not src.is_file():
            raise SystemExit(f"Input file not found: {src}")
        if src.suffix.lower() != ".m4a":
            print(
                "Warning: Voice Memos expects .m4a; continuing anyway.",
                file=sys.stderr,
            )
        label = src.stem if args.label_from_filename else args.label
        if n_in > 1:
            print(f"\n--- File {idx} of {n_in} ---", flush=True)
        run_import_path(src, recordings, db_path, args, label=label)


if __name__ == "__main__":
    main()

"""
fetch_external.py -- Mechanical fetcher for external open-data files.

This is the designated network tool of the harness (design.md 8-5). It is
data-blind: it never reads local analysis data (input/ or any CSV content);
its only input is a fetch plan JSON listing sources to retrieve. Everything it
does is rule-bound: GET-only, https-only, a HARD-CODED host allowlist, cache
discipline, and a machine-generated manifest recording provenance.

Two input modes (mutually exclusive, exactly one required):
    --plan <fetch_plan.json>      An explicit fetch plan (the original mode).
    --from-brief <analysis_brief.json>
                                  Generate the plan internally from an
                                  AnalysisBrief's `external_data` array
                                  (used by the fetch step of preprocessing).

Usage:
    python tools/fetch_external.py --plan <fetch_plan.json>
                                       --out-dir <dir>
                                       --manifest <path>
                                       [--force] [--timeout 60] [--max-mb 200]
    python tools/fetch_external.py --from-brief <analysis_brief.json>
                                       --out-dir <dir>
                                       --manifest <path>
                                       [--force] [--timeout 60] [--max-mb 200]

--from-brief mode (data-blind boundary):
    Reads the `external_data` array of the brief JSON -- and NOTHING else. It
    never consults `csv_path` or any other brief field, and never opens any
    path named inside the brief (the brief is design metadata, not local
    data; that is the boundary of this mode). For each external_data entry it
    generates one plan source internally, so the URL never passes through an
    LLM's hand-transcription:
        source_name         <- entry.source_name
        access_type/url/    <- entry.access (AccessSpec)
          manual_instructions
        provider/usage_terms<- entry
        expected_format     <- entry.access.expected_format (optional)
        filename            <- _derive_filename(source_name, access.expected_format)
                               (direct_url only; deterministic, see that
                               function). MANUAL sources get no filename: they
                               get a drop FOLDER named _derive_dirname(
                               source_name) -- see "Manual sources" below.
    Two direct_url entries resolving to the same filename, or two manual
    entries resolving to the same drop folder, is a usage error (exit 2); the
    tool never silently overwrites or shares a folder. A missing required field
    or a bad access_type is a usage error too -- the schema validates upstream,
    but this mode re-validates (defense in depth). When `external_data` is
    absent or an empty array, the tool prints "no external data requested" and
    exits 0 WITHOUT creating or modifying the manifest. Every generated source
    then flows through the SAME source-processing path as --plan (allowlist ->
    cache -> GET / manual adoption -> manifest merge); that path is unchanged.

Fetch plan (JSON):
    {"sources": [{...}, ...]} where each source carries:
        source_name          (str, required)
        filename             (str, required for direct_url) -- basename only;
                             any path separator ('/' or '\\') or '..' is a
                             usage error (path-traversal guard). FORBIDDEN for
                             manual sources: the tool owns the location (a
                             per-source drop folder) and adopts whatever
                             filename the human's download arrives with, so a
                             prescribed filename would be a second, conflicting
                             source of truth. Supplying one is a usage error.
        access_type          (required) -- "direct_url" | "manual". Any other
                             value is a usage error.
        url                  (required for direct_url) -- https only.
        manual_instructions  (required for manual) -- human steps, transcribed
                             into the manifest.
        provider, usage_terms (optional) -- transcribed verbatim into the
                             manifest (provenance / licensing trail).
        expected_format      (optional) -- "csv" | "xlsx" | "json" | other. For
                             manual sources a mismatch against the adopted
                             file's extension is REPORTED, never blocking.
    Unknown extra fields are ignored (not transcribed).

Host allowlist (HARD-CODED -- deliberately not extensible at runtime):
    The allowed host suffixes are a code constant. There is NO CLI flag,
    environment variable, or config file to widen it; extending the allowlist
    requires a development PR (design.md 8-5).

Processing order per direct_url source:
    1. Cache check BEFORE any network: if out-dir/filename exists and the
       existing manifest's entry for the same source_name has a matching
       sha256, the source is "cached" and the network is never touched
       (--force overrides).
    2. Allowlist check: https scheme + host suffix match. Off-list sources
       fail WITHOUT any network contact.
    3. GET with timeout and size cap. The download streams to a temp file and
       is renamed into place only on success, so no partial file is ever left
       at the final name. Redirects are re-checked hop by hop: leaving the
       allowlist fails the source (redirect_off_allowlist).
    4. On success: sha256 / size_bytes / http_status / content_type /
       fetched_at (ISO 8601 with timezone) are recorded, status "fetched".
    5. HTTP errors / timeouts fail with a short reason. Response BODIES are
       never written to the manifest or stdout (untrusted text must not enter
       artifacts or LLM context).

Manual sources (per-source drop folder -- the tool owns the location):
    Filenames are never matched. Each manual source gets its own drop folder at
        <out-dir>/manual/<_derive_dirname(source_name)>/
    which the tool CREATES on every run (idempotent). The human drops the
    downloaded file into that folder under ANY name -- no renaming, no path to
    type. The tool then adopts whatever single file it finds there.

    Candidate files are the regular files directly inside the folder, excluding
    dotfiles and the OS junk names Thumbs.db / desktop.ini. Subdirectories are
    never searched. Three outcomes:
        0 candidates  -> failed (manual_file_missing); filename null; path is
                         the drop folder, so the caller can relay it verbatim.
        1 candidate   -> status "manual_verified"; filename is the placed
                         file's ACTUAL basename; path points at that file;
                         sha256 / size_bytes recorded. When expected_format is
                         csv/xlsx/json and the adopted extension differs,
                         format_mismatch carries a short description -- purely
                         informational, it NEVER blocks.
        2+ candidates -> failed (manual_multiple_files); filename null; path is
                         the drop folder; manual_files_found lists the found
                         basenames. File CONTENTS are never read or printed.
    manual_instructions is transcribed into the manifest in every case. Re-runs
    are idempotent: a replaced file is simply re-hashed.

Manifest (--manifest path, JSON):
    {"generated_at": <ISO 8601>, "entries": [...]} -- one entry per source.
    Update semantics are a MERGE keyed on source_name: entries for sources in
    this run's plan are replaced with the new result; existing entries for
    sources NOT in the plan are preserved (so provenance survives split runs
    and late manual placement).

    Every entry's `path` is an ABSOLUTE path (posix separators). It is not for
    display: downstream roles transcribe it verbatim and OPEN the file at it,
    so it must carry its own anchor -- a relative path would need one the
    reader does not have.

    Entry fields that manual sources make conditional:
        filename            null while a manual source is unresolved (missing /
                            multiple); the real basename once adopted.
        path                the drop FOLDER while unresolved; the adopted FILE
                            once resolved. Absolute either way. Callers relay
                            it verbatim.
        format_mismatch     manual only, nullable -- e.g. "expected xlsx, got
                            csv". Informational.
        manual_files_found  manual only, present ONLY on manual_multiple_files:
                            the sorted candidate basenames.

stdout:
    One human-readable line per source (source_name / status / reason) plus a
    final tally line. Manual sources add indented follow-up lines carrying the
    drop-folder path and, when unresolved, what the human must do. Never row
    values, never response bodies, never file contents. UTF-8 forced (Windows
    cp932 guard, same pattern as profile_data.py).

Exit codes:
    0  every source is fetched / cached / manual_verified.
    1  at least one source failed (details in the manifest).
    2  usage error -- bad plan JSON, bad arguments, or an invalid filename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Hard-coded allowlist -- the ONLY way to widen this is a development PR
# (design.md 8-5). No CLI flag / env var / config file reads it or extends it.
# Known gap: many municipalities use geographic JP domains (e.g.
# city.<name>.<pref>.jp), which ".lg.jp" does not cover -- deliberately
# excluded in stage 1. Extend via a development PR when a real case needs it
# (off-list fetches fail loudly and can fall back to manual).
# ---------------------------------------------------------------------------

ALLOWED_HOST_SUFFIXES = (".go.jp", ".lg.jp")
ALLOWED_SCHEMES = ("https",)

USER_AGENT = "EBPM-Claude-fetch/1.0"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_MB = 200
_CHUNK_SIZE = 65536

# ---------------------------------------------------------------------------
# Statuses and failure reasons
# ---------------------------------------------------------------------------

STATUS_FETCHED = "fetched"
STATUS_CACHED = "cached"
STATUS_MANUAL_VERIFIED = "manual_verified"
STATUS_FAILED = "failed"

REASON_HOST_NOT_ALLOWED = "host_not_allowed"
REASON_REDIRECT_OFF_ALLOWLIST = "redirect_off_allowlist"
REASON_SIZE_EXCEEDED = "size_exceeded"
REASON_MANUAL_FILE_MISSING = "manual_file_missing"
REASON_MANUAL_MULTIPLE_FILES = "manual_multiple_files"

# Manual drop folders live under this sub-directory of --out-dir, one per
# source. Manual dirnames and direct_url filenames cannot collide: they sit at
# different directory levels.
MANUAL_SUBDIR = "manual"

# Junk the OS sprinkles into folders; never a candidate for adoption. Dotfiles
# are excluded separately (prefix match), these two are exact names.
MANUAL_IGNORED_NAMES = frozenset({"Thumbs.db", "desktop.ini"})

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

ACCESS_DIRECT_URL = "direct_url"
ACCESS_MANUAL = "manual"


# ---------------------------------------------------------------------------
# Errors internal to the fetch path
# ---------------------------------------------------------------------------


class _SizeExceededError(Exception):
    """Download exceeded the --max-mb cap."""


class _RedirectOffAllowlistError(Exception):
    """A redirect target left the host allowlist."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _host_allowed(
    url: str,
    suffixes: tuple[str, ...] = ALLOWED_HOST_SUFFIXES,
    schemes: tuple[str, ...] = ALLOWED_SCHEMES,
) -> bool:
    """Return True iff *url* is https (per *schemes*) on an allowlisted host.

    A suffix beginning with '.' matches by suffix (``".go.jp"`` matches
    ``www.e-stat.go.jp`` and the bare apex ``go.jp``); any other entry must
    match the hostname exactly (used by tests to allow ``127.0.0.1``).

    Pure function: no I/O, no state.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in schemes:
        return False
    host = parts.hostname
    if not host:
        return False
    host = host.lower()
    for suffix in suffixes:
        suffix = suffix.lower()
        if suffix.startswith("."):
            if host == suffix[1:] or host.endswith(suffix):
                return True
        elif host == suffix:
            return True
    return False


def _sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_path(path: Path) -> str:
    """Resolve *path* to an ABSOLUTE path string with posix separators.

    The manifest's ``path`` is not decoration: downstream roles copy it into
    their own artifacts and OPEN the file at it. An absolute path is the only
    form that needs no anchor on the reader's side -- a relative one would
    require the reader to know which directory it is relative to, which the
    analysis layer has no way to learn.

    Separators are normalised to '/' so the manifest reads the same on Windows
    and macOS; Windows accepts forward slashes in paths.
    """
    return path.resolve().as_posix()


def _now_iso() -> str:
    """Current UTC time, ISO 8601 with explicit timezone offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Network I/O -- isolated in this single function (plus the redirect handler
# it installs). Nothing else in this module touches the network.
# ---------------------------------------------------------------------------


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates EVERY hop against the allowlist.

    Raising here means the off-allowlist host is never contacted at all,
    which is strictly stronger than re-checking only the final URL.
    """

    def __init__(self, suffixes: tuple[str, ...], schemes: tuple[str, ...]) -> None:
        self._suffixes = suffixes
        self._schemes = schemes

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not _host_allowed(newurl, self._suffixes, self._schemes):
            raise _RedirectOffAllowlistError(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _https_get(
    url: str,
    timeout: float,
    max_bytes: int,
    dest_path: Path,
    suffixes: tuple[str, ...],
    schemes: tuple[str, ...],
) -> dict:
    """GET *url*, streaming the body to *dest_path*. The sole network function.

    https-only in production: the scheme gate (ALLOWED_SCHEMES) rejects the
    URL before this function is reached, and every redirect hop re-applies it.

    Redirect hops are validated against the allowlist before being followed,
    and the final URL is re-checked as defense in depth.

    Returns:
        ``{"http_status": int, "content_type": str | None, "size_bytes": int}``.

    Raises:
        _SizeExceededError: Content-Length or the streamed byte count exceeds
            *max_bytes*. *dest_path* may hold a partial body; the CALLER owns
            temp-file cleanup (this function is always given a temp path,
            never the final filename).
        _RedirectOffAllowlistError: a redirect left the allowlist.
        urllib.error.HTTPError / urllib.error.URLError / OSError: transport
            failures, propagated for the caller to summarise (bodies are
            never read on error paths).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}, method="GET"
    )
    opener = urllib.request.build_opener(_AllowlistRedirectHandler(suffixes, schemes))
    with opener.open(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise _SizeExceededError(url)
            except ValueError:
                pass  # unparsable header -- the streaming check below still guards
        total = 0
        with open(dest_path, "wb") as fh:
            while True:
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _SizeExceededError(url)
                fh.write(chunk)
        final_url = response.geturl()
        if not _host_allowed(final_url, suffixes, schemes):
            raise _RedirectOffAllowlistError(final_url)
        return {
            "http_status": int(response.status),
            "content_type": response.headers.get("Content-Type"),
            "size_bytes": total,
        }


# ---------------------------------------------------------------------------
# Per-source processing
# ---------------------------------------------------------------------------


def _base_entry(source: dict, path: Path, filename: str | None) -> dict:
    """Manifest entry skeleton shared by every outcome.

    *filename* is None (and *path* is the drop folder) for a manual source that
    has not been resolved to a placed file yet.
    """
    return {
        "source_name": source["source_name"],
        "access_type": source["access_type"],
        "filename": filename,
        "path": _absolute_path(path),
        "url": source.get("url"),
        "provider": source.get("provider"),
        "usage_terms": source.get("usage_terms"),
        "status": None,
        "sha256": None,
        "size_bytes": 0,
        "http_status": None,
        "content_type": None,
        "fetched_at": None,
        "failure_reason": None,
    }


def manual_drop_dir(out_dir: Path, source_name: str) -> Path:
    """The drop folder a manual source's file is adopted from."""
    return out_dir / MANUAL_SUBDIR / _derive_dirname(source_name)


def _manual_candidates(drop_dir: Path) -> list[str]:
    """Sorted basenames of the adoptable files directly inside *drop_dir*.

    Regular files only: subdirectories are never searched (a human who drags a
    folder in gets told so, rather than having its contents silently adopted).
    Dotfiles and OS junk names are ignored.
    """
    return sorted(
        child.name
        for child in drop_dir.iterdir()
        if child.is_file()
        and not child.name.startswith(".")
        and child.name not in MANUAL_IGNORED_NAMES
    )


def _format_mismatch(expected_format: str | None, filename: str) -> str | None:
    """Describe an expected_format / actual-extension mismatch, else None.

    Advisory only -- a mismatch never fails a source. Formats outside
    csv/xlsx/json carry no expectation, so they never mismatch.
    """
    if expected_format not in _FILENAME_FORMATS:
        return None
    actual = Path(filename).suffix.lstrip(".").lower()
    if actual == expected_format:
        return None
    return f"expected {expected_format}, got {actual or '(none)'}"


def _manual_notes(entry: dict) -> list[str]:
    """Indented stdout follow-up lines for one manual source.

    Every manual source reports its location; an unresolved one also states
    exactly what the human must do, because this text is what the orchestrator
    relays. Only names are ever printed -- never file contents.
    """
    reason = entry["failure_reason"]
    if reason == REASON_MANUAL_FILE_MISSING:
        return [
            f"  drop folder: {entry['path']}",
            "  place exactly ONE file directly in this folder. Any filename is "
            "fine -- no renaming needed. Subfolders are not searched.",
        ]
    if reason == REASON_MANUAL_MULTIPLE_FILES:
        found = entry["manual_files_found"]
        return [
            f"  drop folder: {entry['path']}",
            f"  found {len(found)} files: {', '.join(found)}",
            "  leave exactly ONE file directly in this folder. Subfolders are "
            "not searched.",
        ]
    notes = [f"  adopted: {entry['path']}"]
    if entry["format_mismatch"]:
        notes.append(f"  note: {entry['format_mismatch']} (not blocking)")
    return notes


def _process_manual(source: dict, out_dir: Path) -> dict:
    """Adopt the single file a human dropped into this source's drop folder.

    The folder is created on every run (the human must never have to create or
    type a path), then whatever single regular file sits directly inside it is
    adopted under its own name. Zero or several files leave the source failed
    with a reason the caller can act on.
    """
    drop_dir = manual_drop_dir(out_dir, source["source_name"])
    drop_dir.mkdir(parents=True, exist_ok=True)

    entry = _base_entry(source, drop_dir, None)
    entry["manual_instructions"] = source["manual_instructions"]
    entry["format_mismatch"] = None

    candidates = _manual_candidates(drop_dir)
    if not candidates:
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = REASON_MANUAL_FILE_MISSING
        return entry
    if len(candidates) > 1:
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = REASON_MANUAL_MULTIPLE_FILES
        entry["manual_files_found"] = candidates
        return entry

    placed = drop_dir / candidates[0]
    entry["status"] = STATUS_MANUAL_VERIFIED
    entry["filename"] = candidates[0]
    entry["path"] = _absolute_path(placed)
    entry["sha256"] = _sha256_file(placed)
    entry["size_bytes"] = placed.stat().st_size
    entry["format_mismatch"] = _format_mismatch(
        source.get("expected_format"), candidates[0]
    )
    return entry


def process_source(
    source: dict,
    out_dir: Path,
    prior_entry: dict | None,
    *,
    force: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_MB * 1024 * 1024,
    allowed_suffixes: tuple[str, ...] = ALLOWED_HOST_SUFFIXES,
    allowed_schemes: tuple[str, ...] = ALLOWED_SCHEMES,
) -> dict:
    """Process one already-validated plan source and return its manifest entry.

    The allowlist arrives as parameters ONLY so that tests can point the tool
    at a local 127.0.0.1 server; the defaults are the hard-coded production
    constants and the CLI offers no way to override them.

    Args:
        source: One validated source dict from the fetch plan.
        out_dir: Directory a direct_url file is saved into (must exist); a
            manual source's drop folder is created beneath it.
        prior_entry: This source_name's entry from the existing manifest, if
            any -- consulted for the cache check (direct_url only).
        force: Skip the cache check and re-fetch.
        timeout: Socket timeout in seconds for the GET.
        max_bytes: Hard cap on the downloaded body size.
        allowed_suffixes: Host allowlist (default: production constant).
        allowed_schemes: URL schemes accepted (default: https only).

    Returns:
        A complete manifest entry dict (see module docstring).
    """
    if source["access_type"] == ACCESS_MANUAL:
        return _process_manual(source, out_dir)

    # --- direct_url ---
    out_path = out_dir / source["filename"]
    entry = _base_entry(source, out_path, source["filename"])
    url = source["url"]

    # 1. Cache check BEFORE any network activity.
    if (
        not force
        and out_path.is_file()
        and prior_entry is not None
        and prior_entry.get("sha256")
        and _sha256_file(out_path) == prior_entry["sha256"]
    ):
        entry["status"] = STATUS_CACHED
        entry["sha256"] = prior_entry["sha256"]
        entry["size_bytes"] = out_path.stat().st_size
        entry["http_status"] = prior_entry.get("http_status")
        entry["content_type"] = prior_entry.get("content_type")
        entry["fetched_at"] = prior_entry.get("fetched_at")
        return entry

    # 2. Allowlist check -- off-list sources never touch the network.
    if not _host_allowed(url, allowed_suffixes, allowed_schemes):
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = REASON_HOST_NOT_ALLOWED
        return entry

    # 3. GET to a temp file; rename into place only on success so a failed or
    #    oversized download never leaves a partial file at the final name.
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".fetch-", dir=out_dir)
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        meta = _https_get(
            url, timeout, max_bytes, tmp_path, allowed_suffixes, allowed_schemes
        )
    except _SizeExceededError:
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = REASON_SIZE_EXCEEDED
    except _RedirectOffAllowlistError:
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = REASON_REDIRECT_OFF_ALLOWLIST
    except urllib.error.HTTPError as exc:
        # Status code only -- the response body is untrusted and never read.
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = f"http_error_{exc.code}"
        entry["http_status"] = exc.code
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None)
        entry["status"] = STATUS_FAILED
        entry["failure_reason"] = f"network_error: {reason if reason else exc.__class__.__name__}"
    else:
        os.replace(tmp_path, out_path)
        entry["status"] = STATUS_FETCHED
        entry["sha256"] = _sha256_file(out_path)
        entry["size_bytes"] = meta["size_bytes"]
        entry["http_status"] = meta["http_status"]
        entry["content_type"] = meta["content_type"]
        entry["fetched_at"] = _now_iso()
    finally:
        tmp_path.unlink(missing_ok=True)
    return entry


# ---------------------------------------------------------------------------
# Plan validation (all-or-nothing: any structural problem is a usage error
# BEFORE any source is processed, so exit 2 never does partial work)
# ---------------------------------------------------------------------------


def _exit2(message: str) -> None:
    """Print message to stderr and exit with the usage-error code (2)."""
    print(message, file=sys.stderr)
    sys.exit(EXIT_USAGE)


def _valid_filename(filename: str) -> bool:
    """True iff *filename* is a plain basename (no separators, no '..')."""
    if not filename:
        return False
    return not ("/" in filename or "\\" in filename or ".." in filename)


_FILENAME_FORMATS = ("csv", "xlsx", "json")


def _hash_stem(source_name: str) -> str:
    """Opaque but stable stem for names that sanitize to nothing usable."""
    return f"src_{hashlib.sha256(source_name.encode('utf-8')).hexdigest()[:12]}"


def _sanitize_stem(source_name: str) -> str:
    """Deterministically sanitize a source name into a safe path stem.

    Pure function (no I/O). Every character outside ``[A-Za-z0-9._-]`` becomes
    ``_``; runs of ``_`` collapse to one; leading/trailing ``_`` are stripped;
    the result is truncated to 60 characters. An empty result (e.g. an
    all-Japanese source name) falls back to :func:`_hash_stem`.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", source_name)
    stem = re.sub(r"_+", "_", stem).strip("_")[:60].strip("_")
    return stem or _hash_stem(source_name)


def _derive_filename(source_name: str, expected_format: str | None) -> str:
    """Deterministically derive a safe basename from a source name.

    Pure function. Used by --from-brief to name each DOWNLOADED (direct_url)
    file without threading a filename through the brief. Manual sources are
    named by the human's own download -- see :func:`_derive_dirname`.

    The stem is :func:`_sanitize_stem`; the extension is *expected_format* when
    it is csv / xlsx / json, otherwise (unspecified or anything else) ``dat``.
    """
    ext = expected_format if expected_format in _FILENAME_FORMATS else "dat"
    return f"{_sanitize_stem(source_name)}.{ext}"


def _derive_dirname(source_name: str) -> str:
    """Deterministically derive a manual source's drop-folder name (no extension).

    Pure function. Same sanitization as :func:`_derive_filename`'s stem, plus a
    path-traversal guard: ``.`` and ``..`` survive sanitization (both consist of
    allowed characters) and would escape or collapse the drop folder, so such
    stems fall back to the hash form.
    """
    stem = _sanitize_stem(source_name)
    if stem == "." or ".." in stem:
        return _hash_stem(source_name)
    return stem


def _validate_plan(plan: object, plan_path: Path) -> list[dict]:
    """Validate the fetch plan structure; exit 2 with a pointed message if bad.

    ``access_type`` must be ``direct_url`` or ``manual``. A ``filename`` is required for
    direct_url and forbidden for manual (the tool owns the manual location).
    Two manual sources deriving the same drop folder is a usage error, mirroring
    the direct_url filename-collision guard. Unknown extra fields are tolerated.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("sources"), list):
        _exit2(f"Plan '{plan_path}' must be a JSON object with a 'sources' array.")
    sources = plan["sources"]
    dirname_owner: dict[str, str] = {}  # drop folder -> source_name that claimed it
    for i, source in enumerate(sources):
        where = f"Plan '{plan_path}' sources[{i}]"
        if not isinstance(source, dict):
            _exit2(f"{where} must be an object.")
        source_name = source.get("source_name")
        if not isinstance(source_name, str) or not source_name:
            _exit2(f"{where}: 'source_name' is required and must be a non-empty string.")
        access_type = source.get("access_type")
        if access_type not in (ACCESS_DIRECT_URL, ACCESS_MANUAL):
            _exit2(
                f"{where}: 'access_type' must be 'direct_url' or 'manual'; "
                f"got {access_type!r}."
            )

        if access_type == ACCESS_DIRECT_URL:
            if not isinstance(source.get("filename"), str) or not source["filename"]:
                _exit2(
                    f"{where}: 'filename' is required and must be a non-empty string."
                )
            if not _valid_filename(source["filename"]):
                _exit2(
                    f"{where}: 'filename' must be a plain basename -- path "
                    f"separators and '..' are forbidden (got '{source['filename']}')."
                )
            if not isinstance(source.get("url"), str):
                _exit2(
                    f"{where}: 'url' (string) is required when access_type is "
                    f"'direct_url'."
                )
        else:
            if "filename" in source:
                _exit2(
                    f"{where}: 'filename' is no longer accepted for manual sources "
                    f"(got '{source['filename']}'). The tool creates a drop folder "
                    f"per manual source and adopts whatever single file is placed "
                    f"in it, under that file's own name."
                )
            if not isinstance(source.get("manual_instructions"), str):
                _exit2(
                    f"{where}: 'manual_instructions' (string) is required when "
                    f"access_type is 'manual'."
                )
            dirname = _derive_dirname(source_name)
            if dirname in dirname_owner:
                _exit2(
                    f"{where}: derived drop folder '{dirname}' collides between "
                    f"source '{dirname_owner[dirname]}' and '{source_name}'. Rename "
                    f"one source; refusing to share a drop folder."
                )
            dirname_owner[dirname] = source_name

        for field in ("provider", "usage_terms", "expected_format"):
            if field in source and not isinstance(source[field], str):
                _exit2(f"{where}: '{field}' must be a string when present.")
    return sources


# ---------------------------------------------------------------------------
# --from-brief: build plan sources from an AnalysisBrief's external_data ONLY.
# Data-blind boundary: this reads external_data and no other brief field, and
# opens no path named inside the brief. Structural problems / filename
# collisions are usage errors (exit 2) BEFORE any source is processed.
# ---------------------------------------------------------------------------


def _sources_from_brief(brief: object, brief_path: Path) -> list[dict]:
    """Translate a brief's ``external_data`` array into fetch-plan sources.

    Reads ``external_data`` and nothing else (data-blind). Returns the list of
    generated source dicts (empty when external_data is absent or ``[]``). A
    direct_url entry's filename is derived with :func:`_derive_filename`; two
    entries resolving to the same filename is a usage error (exit 2 -- never a
    silent overwrite). Manual entries get NO filename: they are placed by hand
    into a drop folder named by :func:`_derive_dirname`, and the drop-folder
    collision guard lives in :func:`_validate_plan` (which every generated plan
    passes through). Missing required fields and bad access_type are usage
    errors too (the schema validates upstream; this re-validates -- defense in
    depth).
    """
    if not isinstance(brief, dict):
        _exit2(f"Brief '{brief_path}' must be a JSON object.")
    external = brief.get("external_data")
    if external is None:
        external = []
    if not isinstance(external, list):
        _exit2(f"Brief '{brief_path}': 'external_data' must be an array when present.")

    sources: list[dict] = []
    filename_owner: dict[str, str] = {}  # filename -> source_name that claimed it
    for i, entry in enumerate(external):
        where = f"Brief '{brief_path}' external_data[{i}]"
        if not isinstance(entry, dict):
            _exit2(f"{where} must be an object.")
        source_name = entry.get("source_name")
        if not isinstance(source_name, str) or not source_name:
            _exit2(f"{where}: 'source_name' is required and must be a non-empty string.")
        access = entry.get("access")
        if not isinstance(access, dict):
            _exit2(f"{where}: 'access' object is required.")
        access_type = access.get("access_type")
        if access_type not in (ACCESS_DIRECT_URL, ACCESS_MANUAL):
            _exit2(
                f"{where}: access.access_type must be 'direct_url' or 'manual'; "
                f"got {access_type!r}."
            )

        expected_format = access.get("expected_format")
        source: dict = {"source_name": source_name, "access_type": access_type}
        if access_type == ACCESS_DIRECT_URL:
            url = access.get("url")
            if not isinstance(url, str) or not url:
                _exit2(
                    f"{where}: access.url (string) is required when "
                    f"access_type is 'direct_url'."
                )
            filename = _derive_filename(source_name, expected_format)
            if filename in filename_owner:
                _exit2(
                    f"{where}: derived filename '{filename}' collides between source "
                    f"'{filename_owner[filename]}' and '{source_name}'. Rename one "
                    f"source; refusing to overwrite."
                )
            filename_owner[filename] = source_name
            source["filename"] = filename
            source["url"] = url
        else:
            manual_instructions = access.get("manual_instructions")
            if not isinstance(manual_instructions, str) or not manual_instructions:
                _exit2(
                    f"{where}: access.manual_instructions (string) is required "
                    f"when access_type is 'manual'."
                )
            source["manual_instructions"] = manual_instructions
        if isinstance(expected_format, str) and expected_format:
            source["expected_format"] = expected_format

        for field in ("provider", "usage_terms"):
            value = entry.get(field)
            if value is not None:
                if not isinstance(value, str):
                    _exit2(f"{where}: '{field}' must be a string when present.")
                source[field] = value
        sources.append(source)
    return sources


# ---------------------------------------------------------------------------
# Manifest load / merge / save
# ---------------------------------------------------------------------------


def _load_existing_entries(manifest_path: Path) -> list[dict]:
    """Load entries from an existing manifest, or [] if the file is absent.

    A present-but-unreadable manifest is a usage error: silently discarding
    recorded provenance would be worse than stopping.
    """
    if not manifest_path.exists():
        return []
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _exit2(f"Existing manifest '{manifest_path}' is unreadable: {exc}")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        _exit2(
            f"Existing manifest '{manifest_path}' must be a JSON object with "
            f"an 'entries' array."
        )
    return [e for e in manifest["entries"] if isinstance(e, dict)]


def _merge_entries(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge keyed on source_name: new results replace, unmentioned survive."""
    new_by_name = {e["source_name"]: e for e in new}
    merged: list[dict] = []
    for old in existing:
        name = old.get("source_name")
        if name in new_by_name:
            merged.append(new_by_name.pop(name))
        else:
            merged.append(old)
    merged.extend(e for e in new if e["source_name"] in new_by_name)
    return merged


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch external open-data files listed in a fetch plan JSON "
            "(--plan) or generated from an AnalysisBrief's external_data "
            "(--from-brief). GET-only, https-only, hard-coded host allowlist "
            "(extending it requires a development PR -- there is no runtime "
            "override). Writes a provenance manifest; merges with an existing "
            "manifest keyed on source_name. Never reads local analysis data."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan", metavar="JSON", help="Path to an explicit fetch plan JSON."
    )
    mode.add_argument(
        "--from-brief",
        dest="from_brief",
        metavar="JSON",
        help=(
            "Path to an AnalysisBrief JSON; the plan is generated from its "
            "external_data array only (data-blind: no other brief field is "
            "read, no path inside the brief is opened)."
        ),
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="Directory downloaded / manually placed files live in.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        metavar="JSON",
        help="Path the manifest JSON is written to (merged if it exists).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even when the cached file matches the manifest sha256.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"Socket timeout per GET (default {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--max-mb",
        type=int,
        default=DEFAULT_MAX_MB,
        metavar="MB",
        help=f"Hard cap on each downloaded file's size (default {DEFAULT_MAX_MB}).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the fetch_external CLI.

    Returns:
        0 if every source succeeded (fetched / cached / manual_verified),
        1 if any source failed, 2 on usage errors.
    """
    # Force UTF-8 stdout so source names are not garbled under cp932 (every
    # Windows municipal machine) -- same pattern as profile_data.py.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = _parse_args(argv)
    if args.timeout <= 0:
        _exit2("--timeout must be a positive number of seconds.")
    if args.max_mb <= 0:
        _exit2("--max-mb must be a positive number of megabytes.")

    if args.from_brief:
        brief_path = Path(args.from_brief)
        if not brief_path.exists():
            _exit2(f"Brief file not found: '{brief_path}'")
        try:
            with open(brief_path, encoding="utf-8") as fh:
                brief = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _exit2(f"Failed to read brief JSON '{brief_path}': {exc}")
        sources = _sources_from_brief(brief, brief_path)
        if not sources:
            # No external data requested: touch nothing, exit cleanly.
            print("no external data requested")
            return EXIT_OK
        # Defense in depth: run the internally generated plan through the same
        # structural validator the --plan path uses.
        sources = _validate_plan({"sources": sources}, brief_path)
    else:
        plan_path = Path(args.plan)
        if not plan_path.exists():
            _exit2(f"Plan file not found: '{plan_path}'")
        try:
            with open(plan_path, encoding="utf-8") as fh:
                plan = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _exit2(f"Failed to read plan JSON '{plan_path}': {exc}")
        sources = _validate_plan(plan, plan_path)

    manifest_path = Path(args.manifest)
    existing_entries = _load_existing_entries(manifest_path)
    prior_by_name = {
        e.get("source_name"): e for e in existing_entries if e.get("source_name")
    }

    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _exit2(f"Cannot create out-dir '{out_dir}': {exc}")

    max_bytes = args.max_mb * 1024 * 1024
    new_entries: list[dict] = []
    for source in sources:
        entry = process_source(
            source,
            out_dir,
            prior_by_name.get(source["source_name"]),
            force=args.force,
            timeout=args.timeout,
            max_bytes=max_bytes,
        )
        new_entries.append(entry)
        line = f"{entry['source_name']}: {entry['status']}"
        if entry["status"] == STATUS_FAILED:
            line += f" ({entry['failure_reason']})"
        print(line)
        if entry["access_type"] == ACCESS_MANUAL:
            for note in _manual_notes(entry):
                print(note)

    manifest = {
        "generated_at": _now_iso(),
        "entries": _merge_entries(existing_entries, new_entries),
    }
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        _exit2(f"Failed to write manifest '{manifest_path}': {exc}")

    counts = {
        STATUS_FETCHED: 0,
        STATUS_CACHED: 0,
        STATUS_MANUAL_VERIFIED: 0,
        STATUS_FAILED: 0,
    }
    for entry in new_entries:
        counts[entry["status"]] += 1
    print(
        f"total={len(new_entries)} fetched={counts[STATUS_FETCHED]} "
        f"cached={counts[STATUS_CACHED]} "
        f"manual_verified={counts[STATUS_MANUAL_VERIFIED]} "
        f"failed={counts[STATUS_FAILED]}"
    )
    return EXIT_FAILED if counts[STATUS_FAILED] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

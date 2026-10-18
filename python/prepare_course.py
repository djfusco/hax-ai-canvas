"""Prepare a compact, offline instructional review of a large IMSCC archive."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sys
import struct
import tempfile
import zipfile
import zlib

from imscc_import import IMSCCImportError, import_imscc
from course_package import (
    DOCUMENT_EXTENSIONS, MAX_DOCUMENT_BYTES, MAX_JSON_BYTES, MAX_PACKAGE_BYTES,
    MAX_PACKAGE_EXPANDED, MAX_XML_BYTES, MIB, _central_directory_bound,
    bounded_read, inspect_zip, validate_document, validate_xml,
)

MAX_KEPT_BYTES = 64 * MIB
MAX_INSTRUCTION_BYTES = 10 * MIB


def prepare_course(archive_path, output_path, *, max_input_bytes=8 * 1024**3,
                   include_documents=True):
    """Stream selected entries, normalize locally, and atomically publish once.

    No shared parser limits are changed. Excluded bulk media are never opened.
    The original is never modified; existing destinations are always refused.
    """
    source, output = Path(archive_path), Path(output_path)
    if output.exists() or output.is_symlink():
        raise IMSCCImportError("Output already exists; refusing to overwrite it.")
    if source.resolve() == output.resolve():
        raise IMSCCImportError("The original archive cannot be overwritten.")
    if type(max_input_bytes) is not int or not 0 < max_input_bytes <= 8 * 1024**3:
        raise IMSCCImportError("Input limit must be between 1 byte and 8 GiB.")
    temp_output = None
    try:
        if source.stat().st_size > max_input_bytes:
            raise IMSCCImportError("Original archive exceeds the input size limit.")
        _central_directory_bound(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".hax-prepare-", dir=output.parent) as work:
            work = Path(work)
            trimmed = work / "instructional.imscc"
            exclusions = []
            kept_bytes = 0
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(
                    trimmed, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as destination:
                entries = inspect_zip(original)
                if "imsmanifest.xml" not in entries:
                    raise IMSCCImportError("Archive is missing imsmanifest.xml.")
                # Preflight quotas before reading even the first selected entry.
                selected = []
                for name, info in entries.items():
                    suffix = PurePosixPath(name).suffix.lower()
                    reason = None
                    if suffix == ".xml":
                        if info.file_size > MAX_XML_BYTES:
                            raise IMSCCImportError("XML exceeds the 10 MiB safety limit.")
                    elif suffix in (".html", ".htm"):
                        if info.file_size > MAX_INSTRUCTION_BYTES:
                            reason = "instructional text exceeds the 10 MiB per-entry limit"
                    elif include_documents and suffix in DOCUMENT_EXTENSIONS:
                        if info.file_size > MAX_DOCUMENT_BYTES:
                            reason = "optional document exceeds the 4 MiB limit"
                    else:
                        reason = "bulk media or unsupported attachment excluded"
                    if reason:
                        exclusions.append({"path": name, "size": info.file_size, "reason": reason})
                        continue
                    kept_bytes += info.file_size
                    selected.append((name, info, suffix))
                if kept_bytes > MAX_KEPT_BYTES:
                    raise IMSCCImportError("Kept instructional/metadata/document budget exceeds 64 MiB.")
                if len(selected) > 10000:
                    raise IMSCCImportError("Too many retained entries for the bounded normalizer.")
                for name, info, suffix in selected:
                    limit = MAX_DOCUMENT_BYTES if suffix in DOCUMENT_EXTENSIONS else MAX_INSTRUCTION_BYTES
                    data = bounded_read(original, info, limit)
                    if suffix == ".xml":
                        validate_xml(data)
                    elif suffix in DOCUMENT_EXTENSIONS:
                        try:
                            validate_document(data, suffix)
                        except IMSCCImportError:
                            exclusions.append({"path": name, "size": info.file_size,
                                               "reason": "unsupported or active optional document"})
                            continue
                    destination.writestr(name, data)
            summary = import_imscc(trimmed, work / "normalized")
            warnings = summary["warnings"] + [
                "Prepared instructional review only: bulk media excluded; this is not a complete course backup.",
                f"Excluded {len(exclusions)} entries ({sum(e['size'] for e in exclusions)} declared bytes); see exclusion inventory.",
            ]
            manifest = {
                "format": "hax-course-review", "version": 1,
                "course_name": summary["course_name"], "source_type": "prepared",
                "warnings": list(dict.fromkeys(warnings)), "exclusions": exclusions,
                "counts": summary["counts"],
            }
            # Strip machine-local temporary paths from the transport summary.
            summary = {k: v for k, v in summary.items()
                       if k not in ("export_dir", "export_directory", "original_filename")}
            normalized = Path(work / "normalized")
            course = next(normalized.iterdir())
            (course / "export_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False), encoding="utf-8")
            encoded_manifest = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            if len(encoded_manifest) > MAX_JSON_BYTES:
                raise IMSCCImportError("Exclusion inventory exceeds the bounded manifest size.")
            fd, filename = tempfile.mkstemp(prefix=".haxcourse-", dir=output.parent)
            temp_output = Path(filename)
            os.close(fd)
            total = len(encoded_manifest)
            with zipfile.ZipFile(temp_output, "w", compression=zipfile.ZIP_DEFLATED) as package:
                package.writestr("haxcourse.json", encoded_manifest)
                for path in sorted(course.rglob("*")):
                    if path.is_file():
                        size = path.stat().st_size
                        total += size
                        if total > MAX_PACKAGE_EXPANDED or (path.suffix == ".json" and size > MAX_JSON_BYTES):
                            raise IMSCCImportError("Normalized package exceeds review size limits.")
                        package.write(path, "course/" + path.relative_to(course).as_posix())
            if temp_output.stat().st_size > MAX_PACKAGE_BYTES:
                raise IMSCCImportError("Prepared package exceeds the 64 MiB compressed limit.")
            with temp_output.open("rb") as stream:
                os.fsync(stream.fileno())
            # Unlike replace/rename, link fails atomically if a concurrent writer
            # has created the destination. The temporary lives on this filesystem.
            os.link(temp_output, output)
            temp_output.unlink()
            temp_output = None
            return manifest
    except IMSCCImportError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError,
            NotImplementedError, EOFError, struct.error, zlib.error) as exc:
        raise IMSCCImportError(f"Cannot prepare course archive: {exc}") from exc
    finally:
        if temp_output is not None:
            temp_output.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--no-documents", action="store_true",
                        help="Keep instructional HTML/XML only; exclude optional documents")
    args = parser.parse_args(argv)
    print("Inspecting local archive; excluded bulk media will not be read.", file=sys.stderr)
    try:
        result = prepare_course(args.archive, args.output, include_documents=not args.no_documents)
    except IMSCCImportError as exc:
        print(f"Preparation failed: {str(exc)[:500]}", file=sys.stderr)
        return 1
    print("Prepared instructional review (not a full backup).", file=sys.stderr)
    print(json.dumps({
        "output": str(args.output), "course_name": result["course_name"][:200],
        "counts": result["counts"], "excluded_entries": len(result["exclusions"]),
        "excluded_bytes": sum(e["size"] for e in result["exclusions"]),
        "warning_count": len(result["warnings"]),
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

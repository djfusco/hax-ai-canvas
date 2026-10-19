"""Passive local asset checks independent of instructional review size limits."""

import codecs
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile

from course_package import (
    MIB, MAX_XML_BYTES, _central_directory_bound, bounded_read, inspect_zip, validate_xml,
)
from imscc_import import IMSCCImportError

CHUNK = 65536
PDF_ACTIVE = re.compile(
    rb"/(?:JavaScript|JS|Launch|EmbeddedFile|OpenAction|AA|RichMedia|XFA|"
    rb"SubmitForm|ImportData|GoToR|GoToE|AcroForm|ObjStm|Rendition|Movie|Sound|3D)\b", re.I)
PDF_ESCAPE = re.compile(rb"#([0-9a-fA-F]{2})")


def copy_passive_document(archive, info, destination):
    """Validate while copying, with bounded buffers; caller removes failures."""
    suffix = PurePosixPath(info.filename).suffix.lower()
    if suffix in (".docx", ".pptx"):
        # Office ZIP inspection needs seekability, but never a whole-file buffer.
        with tempfile.TemporaryDirectory(prefix=".office-", dir=destination.parent) as work:
            staged = Path(work) / "document.zip"
            with archive.open(info) as source, staged.open("xb") as target:
                _copy(source, target, info.file_size)
            _central_directory_bound(staged)
            try:
                with zipfile.ZipFile(staged) as document:
                    entries = inspect_zip(document, max_entries=2000, max_expanded=32 * MIB)
                    if "[Content_Types].xml" not in entries:
                        raise IMSCCImportError("Invalid Office document.")
                    for name, entry in entries.items():
                        extension = PurePosixPath(name).suffix.lower()
                        if (extension not in {".xml", ".rels", ".png", ".jpg", ".jpeg", ".gif"}
                                or any(part in name.lower() for part in ("embeddings/", "activex/", "vbaproject"))):
                            raise IMSCCImportError("Active or unsupported Office attachment.")
                        if entry.file_size > MIB and entry.file_size > max(1, entry.compress_size) * 200:
                            raise IMSCCImportError("Unsafe Office expansion.")
                        if extension in (".xml", ".rels"):
                            root = validate_xml(bounded_read(document, entry, MAX_XML_BYTES))
                            if any(any(token in node.get("ContentType", "").lower()
                                       for token in ("macroenabled", "vbaproject", "oleobject", "activex"))
                                   for node in root.iter()):
                                raise IMSCCImportError("Active Office content type.")
                            if extension == ".rels" and any(
                                    node.get("TargetMode", "").lower() == "external" for node in root.iter()):
                                raise IMSCCImportError("External Office relationships are not retained.")
                        else:
                            with document.open(entry) as source:
                                # Consume all streams for CRC/declared-length verification.
                                _copy(source, None, entry.file_size)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise IMSCCImportError("Invalid Office document.") from exc
            with staged.open("rb") as source, destination.open("xb") as target:
                _copy(source, target, info.file_size)
        return
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    carry = b""
    total = 0
    with archive.open(info) as source, destination.open("xb") as target:
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            if not total:
                if suffix == ".pdf":
                    if not chunk.startswith(b"%PDF-"):
                        raise IMSCCImportError("Invalid PDF document.")
                elif chunk.startswith((b"SQLite format 3", b"PK\x03\x04", b"MZ")):
                    raise IMSCCImportError("Non-text attachment.")
            total += len(chunk)
            if total > info.file_size:
                raise IMSCCImportError("Document size mismatch.")
            if suffix == ".pdf":
                scan = PDF_ESCAPE.sub(lambda m: bytes([int(m[1], 16)]), carry + chunk)
                if PDF_ACTIVE.search(scan):
                    raise IMSCCImportError("Active PDF document is not retained.")
                # Retain raw bytes to decode #xx tokens split across reads.
                carry = (carry + chunk)[-1024:]
            else:
                if b"\0" in chunk:
                    raise IMSCCImportError("Non-text attachment.")
                try:
                    decoder.decode(chunk)
                except UnicodeError as exc:
                    raise IMSCCImportError("Text attachments must be UTF-8.") from exc
            target.write(chunk)
        if total != info.file_size:
            raise IMSCCImportError("Document size mismatch.")
        if suffix == ".pdf" and not total:
            raise IMSCCImportError("Empty PDF document.")
        if suffix != ".pdf":
            try:
                decoder.decode(b"", final=True)
            except UnicodeError as exc:
                raise IMSCCImportError("Text attachments must be UTF-8.") from exc


def _copy(source, target, expected):
    total = 0
    while True:
        chunk = source.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > expected:
            raise IMSCCImportError("Asset exceeds its declared size.")
        if target:
            target.write(chunk)
    if total != expected:
        raise IMSCCImportError("Asset size mismatch.")

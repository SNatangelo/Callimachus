# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import struct
import zipfile

import pytest

from core.parse import docx_limits, extract
from core.parse.extractors import docx


def _document(path, *, text="Paper accademico — risultati", padding=b""):
    body = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    ).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", body)
        if padding:
            archive.writestr("word/media/large-image.bin", padding)
    return body


def test_large_highly_compressed_embedded_media_does_not_limit_text(tmp_path, monkeypatch):
    path = tmp_path / "paper.docx"
    _document(path, padding=b"x" * (2 * 1024 * 1024))
    monkeypatch.setattr(docx_limits, "MAX_XML_BYTES", 1024)
    assert extract.probe_file(str(path))["detected_format"] == "docx"
    text, fmt, _ = extract.extract_text(str(path))
    assert text == "Paper accademico — risultati"
    assert fmt == "docx"


@pytest.mark.parametrize("part", docx_limits.XML_PARTS)
def test_expanded_xml_rejected_before_zip_read(tmp_path, monkeypatch, part):
    path = tmp_path / "large.docx"
    _document(path)
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        if part == "word/document.xml":
            # Rebuild rather than creating a duplicate XML name.
            pass
        else:
            archive.writestr(part, b"x" * 1025)
    monkeypatch.setattr(docx_limits, "MAX_XML_BYTES", 128 if part == "word/document.xml" else 1024)
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *a, **k: pytest.fail("must reject before decompression"))
    with pytest.raises(ValueError, match="XML exceeds"):
        docx._docx_text(str(path))
    with pytest.raises(ValueError, match="XML exceeds"):
        extract.probe_file(str(path))


def test_combined_xml_limit(tmp_path, monkeypatch):
    path = tmp_path / "combined.docx"
    body = _document(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("word/styles.xml", b"<styles/>")
    monkeypatch.setattr(docx_limits, "MAX_XML_TOTAL_BYTES", len(body))
    with pytest.raises(ValueError, match="combined"):
        docx._docx_text(str(path))


@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_DIRECTORY_BYTES"])
def test_directory_limits_apply_before_zipfile_allocation(tmp_path, monkeypatch, limit):
    path = tmp_path / "directory.docx"
    _document(path)
    monkeypatch.setattr(docx_limits, limit, 1)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **kw: pytest.fail("directory index must not be built"))
    with pytest.raises(ValueError):
        docx._docx_text(str(path))


def test_duplicate_xml_rejected(tmp_path):
    path = tmp_path / "duplicates.docx"
    _document(path)
    with zipfile.ZipFile(path, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("word/document.xml", b"<different/>")
    with pytest.raises(ValueError, match="duplicate"):
        docx._docx_text(str(path))


def test_actual_xml_stream_is_bounded_even_if_size_metadata_lies(monkeypatch):
    import io

    class Archive:
        def open(self, name):
            return io.BytesIO(b"x" * 20)

    monkeypatch.setattr(docx_limits, "MAX_XML_BYTES", 16)
    with pytest.raises(ValueError, match="XML exceeds"):
        docx_limits.read_xml(Archive(), "word/document.xml")


def test_zip64_directory_remains_supported(tmp_path):
    path = tmp_path / "zip64.docx"
    body = _document(path)
    data = path.read_bytes()
    pos = data.rfind(b"PK\x05\x06")
    end = list(struct.unpack("<4s4H2LH", data[pos:pos + 22]))
    size, offset, entries = end[5], end[6], end[4]
    zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                        entries, entries, size, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, pos, 1)
    end[3] = end[4] = 0xffff
    end[5] = end[6] = 0xffffffff
    path.write_bytes(data[:pos] + zip64 + locator + struct.pack("<4s4H2LH", *end))
    docx_limits.check_directory(str(path))
    assert "Paper accademico" in docx._docx_text(str(path))

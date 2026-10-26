#!/usr/bin/env python3
"""Extract and split a document into byte-bounded, overlapping text chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_OVERLAP_BYTES = 2 * 1024
DEFAULT_MAX_CHUNKS = 50

TEXT_EXTENSIONS = {
    ".bat", ".c", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv",
    ".go", ".h", ".hpp", ".htm", ".html", ".ini", ".java", ".js",
    ".json", ".jsonl", ".jsx", ".log", ".markdown", ".md", ".php",
    ".ps1", ".py", ".rb", ".rs", ".rst", ".scss", ".sh", ".sql",
    ".svg", ".tex", ".text", ".toml", ".ts", ".tsv", ".tsx", ".txt",
    ".xml", ".yaml", ".yml",
}

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
W = f"{{{WORD_NS}}}"
A = f"{{{DRAWING_NS}}}"
S = f"{{{SPREADSHEET_NS}}}"
R = f"{{{OFFICE_REL_NS}}}"
PKG_REL = f"{{{PACKAGE_REL_NS}}}"


class ExtractionError(RuntimeError):
    """Raised when a document cannot be converted to usable text."""


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    format_name: str
    method: str
    warnings: list[str]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def decode_text(data: bytes, requested_encoding: str | None = None) -> str:
    if requested_encoding:
        try:
            return data.decode(requested_encoding)
        except UnicodeDecodeError as exc:
            raise ExtractionError(
                f"Could not decode using requested encoding {requested_encoding!r}: {exc}"
            ) from exc

    candidates = ["utf-8-sig"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    candidates.extend(["gb18030"])
    for encoding in candidates:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text
    raise ExtractionError(
        "Could not identify a safe text encoding. Supply --encoding if this is a text document."
    )


def copy_word_text(element: ET.Element) -> str:
    pieces: list[str] = []
    for node in element.iter():
        tag = local_name(node.tag)
        if tag == "t":
            pieces.append(node.text or "")
        elif tag == "tab":
            pieces.append("\t")
        elif tag in {"br", "cr"}:
            pieces.append("\n")
    return "".join(pieces)


def word_table_text(table: ET.Element) -> list[str]:
    lines: list[str] = []
    for row in table.findall(f"{W}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{W}tc"):
            paragraphs = [copy_word_text(p).strip() for p in cell.iter(f"{W}p")]
            cells.append(" ".join(p for p in paragraphs if p))
        if any(cells):
            lines.append("\t".join(cells))
    return lines


def extract_docx(path: Path) -> ExtractionResult:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ExtractionError("The DOCX file is invalid or has no word/document.xml.") from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ExtractionError(f"Could not parse the DOCX XML: {exc}") from exc

    body = root.find(f"{W}body")
    if body is None:
        raise ExtractionError("The DOCX has no document body.")

    lines: list[str] = []
    for child in body:
        tag = local_name(child.tag)
        if tag == "p":
            lines.append(copy_word_text(child))
        elif tag == "tbl":
            lines.extend(word_table_text(child))

    warnings = [
        "DOCX extraction is text-only; images, comments, footnotes, tracked-change views, and exact layout may be absent."
    ]
    return ExtractionResult(
        text="\n".join(lines),
        format_name="docx",
        method="stdlib OOXML text extraction",
        warnings=warnings,
    )


def extract_pptx(path: Path) -> ExtractionResult:
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = [
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ]
            slide_names.sort(key=lambda name: int(re.search(r"(\d+)", name.rsplit("/", 1)[-1]).group(1)))  # type: ignore[union-attr]
            parts: list[str] = []
            for slide_name in slide_names:
                root = ET.fromstring(archive.read(slide_name))
                slide_number = re.search(r"(\d+)", slide_name.rsplit("/", 1)[-1]).group(1)  # type: ignore[union-attr]
                parts.append(f"--- Slide {slide_number} ---")
                for paragraph in root.iter(f"{A}p"):
                    text = "".join(
                        node.text or "" for node in paragraph.iter(f"{A}t")
                    ).strip()
                    if text:
                        parts.append(text)
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise ExtractionError(f"Could not read the PPTX file: {exc}") from exc

    warnings = [
        "PPTX extraction is text-only; slide layout, notes, images, charts, and speaker annotations are not included."
    ]
    return ExtractionResult(
        text="\n\n".join(parts),
        format_name="pptx",
        method="stdlib OOXML slide extraction",
        warnings=warnings,
    )


def excel_column_index(cell_reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", cell_reference)
    if not match:
        return 0
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def extract_xlsx_stdlib(path: Path) -> ExtractionResult:
    try:
        with zipfile.ZipFile(path) as archive:
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            relationship_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))

            relationships = {
                node.get("Id"): node.get("Target", "")
                for node in relationship_root.findall(f"{PKG_REL}Relationship")
            }
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in shared_root.findall(f"{S}si"):
                    shared_strings.append(
                        "".join(node.text or "" for node in item.iter(f"{S}t"))
                    )

            parts: list[str] = []
            for sheet in workbook_root.findall(f".//{S}sheet"):
                sheet_name = sheet.get("name", "Sheet")
                relationship_id = sheet.get(f"{R}id")
                target = relationships.get(relationship_id, "")
                if not target:
                    continue
                if target.startswith("/"):
                    sheet_path = target.lstrip("/")
                else:
                    sheet_path = posixpath.normpath(posixpath.join("xl", target))
                if sheet_path not in archive.namelist():
                    continue

                parts.append(f"## Sheet: {sheet_name}")
                sheet_root = ET.fromstring(archive.read(sheet_path))
                for row in sheet_root.findall(f".//{S}row"):
                    values_by_column: dict[int, str] = {}
                    for cell in row.findall(f"{S}c"):
                        reference = cell.get("r", "")
                        column = excel_column_index(reference)
                        if column < 1:
                            continue
                        cell_type = cell.get("t")
                        if cell_type == "inlineStr":
                            inline = cell.find(f"{S}is")
                            value = (
                                "".join(node.text or "" for node in inline.iter(f"{S}t"))
                                if inline is not None
                                else ""
                            )
                        else:
                            value_node = cell.find(f"{S}v")
                            value = value_node.text if value_node is not None else ""
                            if cell_type == "s" and value:
                                try:
                                    value = shared_strings[int(value)]
                                except (ValueError, IndexError):
                                    pass
                            elif cell_type == "b":
                                value = "TRUE" if value == "1" else "FALSE"
                            elif not value:
                                formula = cell.find(f"{S}f")
                                value = f"={formula.text}" if formula is not None and formula.text else ""
                        values_by_column[column] = value
                    if values_by_column:
                        width = max(values_by_column)
                        parts.append(
                            "\t".join(values_by_column.get(index, "") for index in range(1, width + 1)).rstrip()
                        )
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ExtractionError(f"Could not read the XLSX workbook: {exc}") from exc

    warnings = [
        "XLSX extraction used the dependency-free OOXML fallback; display formatting, dates, drawings, charts, comments, and formulas may be approximate."
    ]
    return ExtractionResult(
        text="\n".join(parts),
        format_name="xlsx",
        method="stdlib OOXML value extraction",
        warnings=warnings,
    )


def extract_xlsx(path: Path) -> ExtractionResult:
    try:
        openpyxl = import_module("openpyxl")
    except ImportError:
        return extract_xlsx_stdlib(path)

    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts: list[str] = []
        try:
            for sheet in workbook.worksheets:
                parts.append(f"## Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    values = ["" if value is None else str(value) for value in row]
                    if any(values):
                        parts.append("\t".join(values).rstrip())
        finally:
            workbook.close()
    except Exception as exc:
        raise ExtractionError(f"Could not read the XLSX workbook: {exc}") from exc

    warnings = [
        "Spreadsheet extraction is value-only; formatting, drawings, charts, comments, and formulas may be absent."
    ]
    return ExtractionResult(
        text="\n".join(parts),
        format_name="xlsx",
        method="openpyxl read-only value extraction",
        warnings=warnings,
    )

def extract_pdf(path: Path) -> ExtractionResult:
    errors: list[str] = []
    blank_result: ExtractionResult | None = None

    for module_name in ("pypdf", "PyPDF2", "fitz", "pdfplumber"):
        try:
            reader_module = import_module(module_name)
        except ImportError:
            continue

        try:
            if module_name in {"pypdf", "PyPDF2"}:
                reader = reader_module.PdfReader(str(path))
                page_texts = [page.extract_text() or "" for page in reader.pages]
                method = f"{module_name} page text extraction"
            elif module_name == "fitz":
                document = reader_module.open(str(path))
                try:
                    page_texts = [page.get_text("text") or "" for page in document]
                finally:
                    document.close()
                method = "PyMuPDF page text extraction"
            else:
                with reader_module.open(str(path)) as document:
                    page_texts = [page.extract_text() or "" for page in document.pages]
                method = "pdfplumber page text extraction"
        except Exception as exc:
            errors.append(f"{module_name}: {exc}")
            continue

        formatted_pages = [
            f"--- Page {index} ---\n{text}" for index, text in enumerate(page_texts, start=1)
        ]
        result = ExtractionResult(
            text="\n\n".join(formatted_pages),
            format_name="pdf",
            method=method,
            warnings=[],
        )
        if any(text.strip() for text in page_texts):
            return result
        if blank_result is None:
            blank_result = result

    if blank_result is not None:
        blank_result.warnings.append(
            "No PDF text was extracted. The file may be scanned or image-only; use OCR or the PDF/vision workflow."
        )
        return blank_result
    if errors:
        raise ExtractionError("Could not read the PDF with available readers: " + "; ".join(errors))
    raise ExtractionError(
        "PDF extraction requires pypdf, PyPDF2, PyMuPDF, or pdfplumber. Use the PDF workflow to export text, or install one reader."
    )

def extract_document(path: Path, requested_encoding: str | None) -> ExtractionResult:
    extension = path.suffix.lower()
    if extension == ".docx":
        return extract_docx(path)
    if extension == ".pptx":
        return extract_pptx(path)
    if extension == ".xlsx":
        return extract_xlsx(path)
    if extension == ".pdf":
        return extract_pdf(path)
    if extension in {".doc", ".xls", ".ppt"}:
        raise ExtractionError(
            f"Legacy binary format {extension} is not supported. Convert it to DOCX/XLSX/PPTX or export text first."
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExtractionError(f"Could not read the input file: {exc}") from exc
    if extension not in TEXT_EXTENSIONS and b"\x00" in data[:8192]:
        raise ExtractionError(
            f"Unsupported binary document type {extension or '(no extension)'}. Extract text first."
        )
    text = decode_text(data, requested_encoding)
    return ExtractionResult(
        text=text,
        format_name=extension.lstrip(".") or "text",
        method="direct text decoding",
        warnings=[],
    )


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def choose_cut(text: str, start: int, hard_end: int) -> int:
    if hard_end - start <= 1:
        return hard_end
    floor = start + max(1, int((hard_end - start) * 0.60))

    paragraph = text.rfind("\n\n", floor, hard_end)
    if paragraph != -1:
        return paragraph + 2

    newline = text.rfind("\n", floor, hard_end)
    if newline != -1:
        return newline + 1

    sentence_candidates = [
        text.rfind(mark, floor, hard_end) for mark in ".!?。！？；;"
    ]
    sentence = max(sentence_candidates)
    if sentence != -1:
        return sentence + 1

    whitespace_candidates = [
        text.rfind(mark, floor, hard_end) for mark in (" ", "\t")
    ]
    whitespace = max(whitespace_candidates)
    if whitespace != -1:
        return whitespace + 1

    return hard_end


def byte_limited_end(text: str, start: int, max_bytes: int) -> int:
    used = 0
    index = start
    while index < len(text):
        char_bytes = len(text[index].encode("utf-8"))
        if used + char_bytes > max_bytes:
            break
        used += char_bytes
        index += 1
    if index == start:
        raise ExtractionError(
            "max-bytes is too small for the next Unicode character; increase --max-bytes."
        )
    return index


def overlap_start(text: str, end: int, overlap_bytes: int) -> int:
    if overlap_bytes <= 0:
        return end
    used = 0
    index = end
    while index > 0:
        char_bytes = len(text[index - 1].encode("utf-8"))
        if used + char_bytes > overlap_bytes:
            break
        used += char_bytes
        index -= 1
    return index


def split_text(text: str, max_bytes: int, overlap_bytes: int, max_chunks: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    start = 0
    previous_end = 0
    total = len(text)
    if total == 0:
        raise ExtractionError("The extracted document is empty.")

    while start < total:
        hard_end = byte_limited_end(text, start, max_bytes)
        if hard_end >= total:
            end = total
        else:
            end = choose_cut(text, start, hard_end)
        if end <= start:
            raise ExtractionError("Internal chunking error: no forward progress.")
        content = text[start:end]
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > max_bytes:
            raise ExtractionError("Internal chunking error: byte budget exceeded.")
        chunks.append(
            {
                "id": f"{len(chunks) + 1:04d}",
                "path": f"chunks/{len(chunks) + 1:04d}.txt",
                "start_char": start,
                "end_char": end,
                "characters": len(content),
                "bytes": content_bytes,
                "overlap_chars_with_previous": max(0, previous_end - start),
                "content": content,
            }
        )
        if len(chunks) > max_chunks:
            raise ExtractionError(
                f"The document would exceed --max-chunks={max_chunks}. "
                "Increase the limit deliberately, raise --max-bytes if safe, or narrow the task."
            )
        if end >= total:
            break
        previous_end = end
        start = max(start + 1, overlap_start(text, end, overlap_bytes))

    return chunks


def make_output_directory(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ExtractionError(f"Output path exists and is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ExtractionError(
                f"Output directory is not empty: {output_dir}. Use a new directory to avoid stale chunks."
            )
    else:
        output_dir.mkdir(parents=True)
    (output_dir / "chunks").mkdir()
    (output_dir / "partials").mkdir()


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_bytes < 256:
        raise ExtractionError("--max-bytes must be at least 256.")
    if args.overlap_bytes < 0 or args.overlap_bytes >= args.max_bytes // 2:
        raise ExtractionError("--overlap-bytes must be non-negative and less than half of --max-bytes.")
    if args.max_chunks < 1:
        raise ExtractionError("--max-chunks must be at least 1.")

    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise ExtractionError(f"Input is not a file: {source}")
    output_dir = args.output_dir.expanduser().resolve()

    source_bytes = source.stat().st_size
    hasher = hashlib.sha256()
    with source.open("rb") as source_stream:
        for block in iter(lambda: source_stream.read(1024 * 1024), b""):
            hasher.update(block)
    source_hash = hasher.hexdigest()
    extraction = extract_document(source, args.encoding)
    text = normalize_newlines(extraction.text)
    chunks = split_text(
        text=text,
        max_bytes=args.max_bytes,
        overlap_bytes=args.overlap_bytes,
        max_chunks=args.max_chunks,
    )

    make_output_directory(output_dir)
    for chunk in chunks:
        chunk_path = output_dir / chunk.pop("path")
        chunk_path.write_bytes(chunk["content"].encode("utf-8"))
        chunk["path"] = str(chunk_path.relative_to(output_dir)).replace("\\", "/")
        chunk.pop("content")

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source),
            "size_bytes": source_bytes,
            "sha256": source_hash,
            "format": extraction.format_name,
        },
        "extraction": {
            "method": extraction.method,
            "text_characters": len(text),
            "text_bytes": len(text.encode("utf-8")),
            "newline_normalization": "CRLF and CR normalized to LF",
            "warnings": extraction.warnings,
        },
        "settings": {
            "max_chunk_bytes": args.max_bytes,
            "overlap_bytes": args.overlap_bytes,
            "max_chunks": args.max_chunks,
        },
        "coverage": {
            "first_char": 0,
            "last_char": len(text),
            "complete": chunks[-1]["end_char"] == len(text),
        },
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "success": True,
        "manifest": str(manifest_path),
        "output_dir": str(output_dir),
        "source": str(source),
        "format": extraction.format_name,
        "source_bytes": source_bytes,
        "text_characters": len(text),
        "text_bytes": len(text.encode("utf-8")),
        "chunk_count": len(chunks),
        "warnings": extraction.warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a document and split it into byte-bounded overlapping text chunks."
    )
    parser.add_argument("input", type=Path, help="DOCX, PPTX, XLSX, PDF, or text-like input file")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New directory for chunks and manifest"
    )
    parser.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="UTF-8 byte ceiling per chunk"
    )
    parser.add_argument(
        "--overlap-bytes",
        type=int,
        default=DEFAULT_OVERLAP_BYTES,
        help="UTF-8 byte overlap between adjacent chunks",
    )
    parser.add_argument(
        "--max-chunks", type=int, default=DEFAULT_MAX_CHUNKS, help="Hard chunk-count limit"
    )
    parser.add_argument("--encoding", help="Explicit encoding for a text file")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable summary")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Created {result['chunk_count']} chunks.")
        print(f"Manifest: {result['manifest']}")
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





---
name: large-document-chunker
description: Handle oversized documents that trigger request-body limits or HTTP 413 Payload Too Large by extracting text locally, splitting it into byte-safe overlapping chunks, processing chunks incrementally, and merging verified results. Use when a DOCX, PDF, presentation, spreadsheet, or text-like document is too large to read or submit in one request; do not activate for ordinary small documents.
metadata:
  short-description: Split and process oversized documents safely
---

# Large Document Chunker

## Preserve the source and avoid a second transport failure

A Codex skill cannot intercept the app's HTTP transport or catch a 413 before the request is sent. After a transport 413, continue in a new turn with the document saved locally and avoid resubmitting the attachment. Treat the error as a signal to stop submitting the whole document, then work from its local path. Never retry the same oversized payload, truncate it silently, or replace the original.

Inspect the file size and type before opening its contents. Do not run a command that prints the entire document into the conversation. Keep generated chunks and partial results in a task-specific temporary directory.

## Prepare byte-safe chunks

Run the bundled script by its absolute path:

```bash
python "<path-to-large-document-chunker>/scripts/chunk_document.py" "<input-file>" --output-dir "<task-work-dir>" --json
```

For a known restrictive gateway, lower the budget rather than guessing: `--max-bytes 32768 --overlap-bytes 1024`. Defaults are 64 KiB per chunk, 2 KiB overlap, and 50 chunks. The hard limit is intentional; if it is exceeded, report the estimated scale and ask for scope reduction rather than dropping content.

The script handles common text formats, DOCX, and PPTX without extra packages. It tries OpenPyXL or a built-in OOXML fallback for XLSX, and tries pypdf, PyPDF2, PyMuPDF, or pdfplumber for PDF. It writes `manifest.json` plus ordinal chunk files. Keep `manifest.json` as the source of truth for ordering, coverage, source hash, and settings.

Read [references/processing-playbook.md](references/processing-playbook.md) before processing chunks unless the task is only to create and inspect the split. Follow its task-specific mode, prompt, merge, and verification rules.

## Process incrementally

Process one chunk at a time. Do not load every chunk into context in one request, and do not ask each chunk to write a whole-document summary. Save each result under a stable ID in a `partials/` directory and record which chunk it covers.

For work that depends on the whole document, use a map-reduce flow: extract per-chunk facts or edits first, then merge those compact results. Merge partial results in bounded batches; if even the batch is too large, merge hierarchically.

Before returning a final answer:

- Confirm that every chunk ID in the manifest has a result.
- Check that the first and last ranges cover the extracted document.
- Remove overlap-induced repetition without dropping distinct facts.
- For edits, verify that reconstructed sections preserve order and boundary context.
- Warn when the task requires exact visual layout, images, scanned text, tracked changes, or cross-chunk reasoning that text extraction cannot preserve.
- State which limits were used only when they materially affect confidence or completeness.

## Handle 413 after chunking

If a single chunk still triggers 413, rerun into a new output directory with half the previous `--max-bytes` and a proportionally smaller overlap. Do not loop indefinitely: after two reductions, stop and report that the upstream body limit is below a practical operating size. For context-window or token errors rather than body-size errors, reduce both chunk size and the requested output per chunk.


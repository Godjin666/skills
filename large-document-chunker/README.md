# Large Document Chunker

[中文说明](README.zh-CN.md)

A Codex skill for handling documents that are too large to extract, submit, or process in one request. It extracts text locally, splits it into UTF-8 byte-bounded chunks with overlap, processes the chunks incrementally, and helps merge the results without silently dropping content.

## Why this skill exists

Large attachments can fail before a model sees them, for example with:

```text
unexpected status 413 Payload Too Large: Failed to buffer the request body: length limit exceeded
```

A Codex skill cannot intercept the application's HTTP transport or catch that error before the request is sent. The practical workflow is therefore:

1. Save the document locally.
2. Start a new turn and work from the local file path instead of resubmitting the oversized attachment.
3. Let this skill extract and split the document.
4. Process one chunk at a time and merge the verified results.

## Features

- Splits by UTF-8 bytes rather than character count, so Chinese text and other multibyte content stay close to the configured request budget.
- Prefers paragraph, line, and sentence boundaries; only falls back to a hard cut when necessary.
- Keeps an overlap between adjacent chunks to reduce context loss at boundaries.
- Writes a machine-readable `manifest.json` containing source size, SHA-256, chunk ranges, overlap, and coverage.
- Processes chunks incrementally with resumable partial-result files.
- Supports hierarchical merging so the merge step does not recreate the original 413 problem.
- Applies a hard chunk limit to prevent runaway token and time costs.
- Never modifies the source document.

## Supported formats

| Format | Support |
| --- | --- |
| TXT, Markdown, code, JSON, CSV, XML, HTML, and similar text files | Built in |
| DOCX | Built in OOXML text extraction |
| PPTX | Built in OOXML slide text extraction |
| XLSX | OpenPyXL when available, otherwise a dependency-free OOXML fallback |
| PDF | Requires `pypdf`, `PyPDF2`, `PyMuPDF`, or `pdfplumber` |

Extraction is text-oriented. Images, scanned pages, exact page layout, comments, tracked-change views, annotations, and charts may not survive. Use the appropriate document, PDF, or vision workflow when those elements matter.

## Requirements

- Python 3.10 or newer.
- No third-party package is required for plain text, DOCX, or PPTX.
- A supported PDF reader is required for PDF input.
- `openpyxl` is optional for higher-fidelity XLSX value extraction.

## Installation

Copy the `large-document-chunker` directory into a Codex-discoverable skills directory, for example:

```text
~/.codex/skills/large-document-chunker/
```

or, on this installation:

```text
~/.agents/skills/large-document-chunker/
```

Keep this structure:

```text
large-document-chunker/
|-- SKILL.md
|-- README.md
|-- README.zh-CN.md
|-- agents/
|   `-- openai.yaml
|-- references/
|   `-- processing-playbook.md
`-- scripts/
    `-- chunk_document.py
```

## Usage

Ask Codex to use the skill with a local document path:

```text
Use $large-document-chunker to summarize C:\work\report.docx
```

You can also run the splitter directly:

```bash
python scripts/chunk_document.py "C:\work\report.docx" --output-dir "C:\work\report-chunks" --json
```

The output directory contains:

```text
report-chunks/
|-- manifest.json
|-- chunks/
|   |-- 0001.txt
|   |-- 0002.txt
|   `-- ...
`-- partials/
```

`manifest.json` is the source of truth for ordering and coverage. Save one concise result per chunk under `partials/` while processing.

## Options

| Option | Default | Purpose |
| --- | ---: | --- |
| `--max-bytes` | `65536` | Maximum UTF-8 bytes in each chunk |
| `--overlap-bytes` | `2048` | Maximum overlap between adjacent chunks |
| `--max-chunks` | `50` | Hard limit on the number of chunks |
| `--encoding` | Auto-detect | Explicit encoding for a text file |
| `--json` | Off | Print a machine-readable execution summary |

For a known restrictive gateway, try a smaller budget:

```bash
python scripts/chunk_document.py "input.md" --output-dir "split" --max-bytes 32768 --overlap-bytes 1024 --json
```

The script refuses to write into a non-empty output directory. Create a new directory for each retry so stale chunks cannot be mixed with fresh ones.

## Processing model

1. Inspect file size, type, and the user's goal before reading the whole file.
2. Extract text and verify the generated manifest.
3. Choose a task-specific mode: summary/review, search, extraction, editing, or cross-document comparison.
4. Process one chunk at a time and persist compact partial results.
5. Merge partials in bounded batches, recursively if necessary.
6. Verify every chunk is covered and no content was silently omitted.
7. Warn when layout, images, scans, or distant cross-chunk relationships make the result less reliable.

For detailed prompts and merge rules, see [references/processing-playbook.md](references/processing-playbook.md).

## If 413 persists

If one chunk still triggers 413:

- Stop retrying the same payload.
- Create a new output directory.
- Halve `--max-bytes` and reduce the overlap proportionally.
- After two reductions, stop and report that the upstream body limit is too small for practical processing.

A body-size 413 and a model context-window error are different failures. For context or token errors, reduce both the chunk size and the amount of output requested per chunk.

## License

No separate license is included in this skill. Add one before redistributing if required by your repository or organization.

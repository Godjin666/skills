# Processing playbook

Use this after `scripts/chunk_document.py` has produced `manifest.json`.

## 1. Choose the processing mode

Infer the mode from the user's requested outcome:

- **Summary or review:** Extract concise facts, claims, issues, or section notes per chunk. Merge by topic or source order.
- **Question answering or search:** Locate candidate chunks first. Read only relevant chunks and their nearby overlap. Do not summarize unrelated chunks.
- **Extraction or transformation:** Ask for structured partial output keyed by source range. Preserve IDs and source order.
- **Editing:** Analyze first, then apply edits to a copy of the original document. Use text chunks only for locating changes. Keep formatting and document-specific structure out of text-only reconstruction.
- **Cross-document comparison:** Build a compact normalized record per document first; merge only the compact records.

Whole-document reasoning is fundamentally weaker after text-only extraction and chunking. Say so when the requested answer depends on subtle layout, images, or relations spanning distant sections.

## 2. Use a chunk prompt that prevents duplicate summaries

Use a bounded prompt like this:

```text
Source: <file name>
Chunk: <id> of <total>
Covered range: characters <start>-<end> (may overlap adjacent chunks)
Document-wide task: <user's task>

Process only this chunk. Return compact findings relevant to the task.
Attribute each finding to this chunk ID. Note uncertainty or content that appears
to continue across a boundary. Do not summarize the whole document and do not
claim knowledge of chunks you have not received.
```

For editing, require a stable location anchor plus the proposed change. Do not let each chunk rewrite unrelated sections.

## 3. Save resumable partials

Store one Markdown file per chunk, for example `partials/0007.md`:

```markdown
---
chunk_id: "0007"
source_range: [7340032, 7405568]
---
- Finding with a short supporting detail.
- Ambiguity or boundary issue.
```

Keep partials concise. Persist progress as work proceeds so an interrupted run can resume without redoing completed chunks.

## 4. Merge with a budget

Gather partials into merge batches based on byte size, not a fixed count. Keep a conservative merge prompt budget (often 32-64 KiB), and recurse if the merged text would exceed it. The merge prompt should:

- preserve source order;
- identify and collapse overlap repetition;
- retain disagreements and uncertainty;
- avoid inventing missing sections;
- return the final artifact directly when the task is simple.

At the final stage, validate against `manifest.json`: every chunk accounted for, contiguous coverage, no truncation, and no accidental use of stale chunks.

## 5. Failure handling

- **413 on a chunk:** recreate chunks with half the previous byte budget; do not retry the same payload.
- **Context-length error:** reduce chunk bytes and cap per-chunk output.
- **Timeout or rate limit:** resume from the last saved partial; do not reorder chunks.
- **Extraction warnings:** inspect the manifest. For scanned PDFs or image-heavy documents, use the appropriate document/vision workflow instead of assuming extracted text is complete.
- **Too many chunks:** narrow the task, process selected ranges, or request a smaller excerpt. Never silently omit chunks.

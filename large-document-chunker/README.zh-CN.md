# 超大文档分片处理

[English](README.md)

这是一个用于处理超大文档的 Codex 技能。当文档过大，无法在一次请求中读取、提交或完整处理时，本技能会先在本地抽取文本，再按照 UTF-8 字节数切分为带重叠的分片，逐片处理并可靠合并，避免静默截断或遗漏内容。

## 为什么需要这个技能

大型附件可能在模型看到内容之前就失败，例如：

```text
unexpected status 413 Payload Too Large: Failed to buffer the request body: length limit exceeded
```

Codex Skill 无法拦截应用程序的 HTTP 传输层，也不能在请求发出前捕获该错误。因此可靠的处理方式是：

1. 将文档保存到本地。
2. 新开一轮对话，提供本地文件路径，不要重新提交同一份超大附件。
3. 由本技能抽取文本并生成分片。
4. 逐个处理分片，校验并合并结果。

## 主要能力

- 按 UTF-8 字节数切分，而不是只按字符数估算，更适合中文及其他多字节内容。
- 优先在段落、换行和句子边界切分，必要时才进行硬切。
- 相邻分片保留重叠窗口，减少边界处的上下文丢失。
- 生成 `manifest.json`，记录源文件大小、SHA-256、分片范围、重叠和覆盖情况。
- 逐片处理并保存可恢复的中间结果，中断后不必从头开始。
- 支持分层合并，避免合并阶段再次触发请求体过大。
- 设置最大分片数，防止超大文档无限消耗时间和 token。
- 不修改原始文档。

## 支持格式

| 格式 | 支持情况 |
| --- | --- |
| TXT、Markdown、代码、JSON、CSV、XML、HTML 等文本文件 | 内置支持 |
| DOCX | 使用内置 OOXML 文本提取 |
| PPTX | 使用内置 OOXML 幻灯片文本提取 |
| XLSX | 优先使用 OpenPyXL；未安装时使用内置 OOXML 兜底 |
| PDF | 需要 `pypdf`、`PyPDF2`、`PyMuPDF` 或 `pdfplumber` 之一 |

本技能以提取文字为主。图片、扫描页、精确版式、批注、修订视图、图表等内容可能无法保留。如果这些内容对任务很重要，应配合相应的文档、PDF 或视觉识别流程。

## 环境要求

- Python 3.10 或更高版本。
- 纯文本、DOCX 和 PPTX 不需要第三方包。
- 处理 PDF 时需要安装一种受支持的 PDF 读取器。
- `openpyxl` 为可选依赖，可提高 XLSX 数值提取的兼容性。

## 安装

将整个 `large-document-chunker` 目录复制到 Codex 能识别的技能目录，例如：

```text
~/.codex/skills/large-document-chunker/
```

或者：

```text
~/.agents/skills/large-document-chunker/
```

目录结构应保持为：

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

## 使用方式

直接让 Codex 使用本技能处理本地文档：

```text
使用 $large-document-chunker 总结 C:\work\报告.docx
```

也可以手动运行分片脚本：

```powershell
python scripts/chunk_document.py "C:\work\报告.docx" --output-dir "C:\work\报告-chunks" --json
```

输出目录包含：

```text
报告-chunks/
|-- manifest.json
|-- chunks/
|   |-- 0001.txt
|   |-- 0002.txt
|   `-- ...
`-- partials/
```

`manifest.json` 是分片顺序和覆盖范围的依据。处理时应在 `partials/` 中为每个分片保存一份简短的中间结果。

## 参数说明

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `--max-bytes` | `65536` | 每个分片允许的最大 UTF-8 字节数 |
| `--overlap-bytes` | `2048` | 相邻分片的最大重叠字节数 |
| `--max-chunks` | `50` | 分片数量硬上限 |
| `--encoding` | 自动检测 | 为文本文件指定编码 |
| `--json` | 关闭 | 输出机器可读的执行摘要 |

如果已经知道网关限制较严格，可以先采用更小的预算：

```powershell
python scripts/chunk_document.py "input.md" --output-dir "split" --max-bytes 32768 --overlap-bytes 1024 --json
```

脚本不会写入非空输出目录。每次重试都应创建新目录，避免旧分片与新分片混在一起。

## 处理流程

1. 先检查文件大小、类型和用户目标，不要一开始就输出整份文档。
2. 抽取文本并检查生成的 manifest。
3. 根据任务选择处理模式：摘要或审阅、检索问答、结构化提取、文档编辑或跨文档比较。
4. 一次处理一个分片，并及时保存简短的中间结果。
5. 按字节预算分批合并中间结果；必要时进行多层合并。
6. 确认所有分片都已处理、范围连续且没有静默遗漏。
7. 如果任务依赖版式、图片、扫描文字或相隔较远章节之间的关系，应明确说明分片处理带来的可靠性限制。

详细提示词、合并规则和异常处理见 [references/processing-playbook.md](references/processing-playbook.md)。

## 仍然触发 413 时

如果单个分片仍然触发 413：

- 停止重复提交相同内容。
- 创建新的输出目录。
- 将 `--max-bytes` 减半，并按比例减小重叠。
- 连续缩小两次后仍失败，应停止并说明上游请求体限制已低于可实际使用的范围。

请求体过大与服务端上下文窗口溢出是两类不同错误。若遇到上下文长度或 token 限制，应同时减小分片大小和每片要求的输出量。

## 许可证

本技能目前没有单独附带开源许可证。如果仓库或组织要求许可证，请在再分发前补充。

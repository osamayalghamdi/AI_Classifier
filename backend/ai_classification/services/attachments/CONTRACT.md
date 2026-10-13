# Attachment layers — CONTRACT

Every attachment (screenshot, PDF, receipt, letter, exported list, photo) is
reduced to **one** `AttachmentLayers` record. The reader is external and
configured entirely by environment; this contract is what the rest of the
system consumes.

## The layers

| Layer | Question | Notes |
|---|---|---|
| `where` | Where is this? | portal, page, page_en, section, url, external_source — all optional |
| `objects` | Which business objects? | `type` (open vocabulary, snake_case), `id`, `label_as_shown`, `attributes` |
| `signals` | What is abnormal or decision-relevant? | `type` (error / warning / status / disabled_control / empty_state / value / other — open), `code`, `text`, `text_en`, `about` |
| `text` | Full transcription | original language, line breaks preserved |
| `summary_en` | One plain English sentence | |
| `quality` | `clear` / `partial` / `unreadable` | |

Every field is optional and every list may be empty. An error is just one kind
of signal: an empty list, a disabled button or a stale status are signals too.

## Validation (in code, never in the prompt)

- Unknown extra keys from the model are dropped, not fatal.
- Strings are trimmed and length-capped: `text` ≤ 8000, everything else ≤ 300.
- `quality="unreadable"` keeps only `summary_en` + `quality`.
- **Grounding:** a `signals[].code` or `objects[].id` that does not appear in
  `text` (after normalization) is dropped and logged as
  `attachment_ungrounded_value`.

## Normalization

Uppercase, Arabic-Indic digits → ASCII, drop spaces/dashes/underscores.
`TX-778201`, `tx 778201` and `TX778201` all collapse to `TX778201`.

## Reader (external, env-only)

- `ATTACHMENT_READER_MODE=vision_llm` (implemented): one OpenAI-compatible
  chat-completions call; the image is a `data:<mime>;base64` URL; the prompt is
  in `prompts.py` (version `ATTACHMENT_PROMPT_VERSION`). URL/model/key have **no
  defaults** — set `ATTACHMENT_VISION_API_URL`, `ATTACHMENT_VISION_MODEL`,
  `ATTACHMENT_VISION_API_KEY`.
- `ocr_then_llm`: a stub (raises NotImplementedError) — not implemented.
- Accepts `image/*` and `application/pdf` only; PDFs are rendered with
  pypdfium2, capped at `ATTACHMENT_MAX_PDF_PAGES`. Anything else → `unreadable`,
  no provider call.
- Missing config while enabled → one loud startup warning, text-only. An
  extraction error never fails the ticket: it becomes `unreadable`.

## Environment

`ATTACHMENTS_ENABLED`, `ATTACHMENT_READER_MODE`, `ATTACHMENT_VISION_API_URL`,
`ATTACHMENT_VISION_MODEL`, `ATTACHMENT_VISION_API_KEY`, `ATTACHMENT_OCR_API_URL`,
`ATTACHMENT_OCR_API_KEY`, `ATTACHMENT_TIMEOUT_S`, `ATTACHMENT_MAX_PER_TICKET`,
`ATTACHMENT_MAX_BYTES`, `ATTACHMENT_MAX_PDF_PAGES`, `ATTACHMENT_CONCURRENCY`,
`ATTACHMENT_IN_PROMPT`, `ATTACHMENT_LINKING` — see `.env.example`.

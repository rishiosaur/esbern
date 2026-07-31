---
name: manage-esbern-library
description: Search, browse, and add books to the owner's hosted Esbern library from ChatGPT or Claude, including checking background installation and reMarkable sync jobs. Use when the user asks what books are in their library, whether they already own a title, or asks to add, fetch, download, or install a book into their library.
---

# Manage Esbern Library

Use the hosted Esbern tools exposed by the assistant. Do not run a local CLI or
read local folders.

## Choose the operation

- To see everything, call `get_full_library` (Claude) or `listLibrary`
  (ChatGPT).
- To find a book, call `search_library` or `searchLibrary`. Search by the most
  distinctive available title and author terms.
- To add a book, search first. If there is a strong title-and-author match,
  explain that it is already present and do not enqueue a duplicate.
- If the requested edition or title is ambiguous, ask one short clarifying
  question before adding it.
- Only call `add_book` or `queueBook` after the user explicitly asks to add,
  fetch, download, or install the book. This starts an external download and a
  reMarkable sync.
- Report the returned job ID and initial status. Use `check_book_job` or
  `getBookJob` when the user asks for progress, or once after enqueueing when
  the tool can be called without delaying the response.

Use `auto` format unless the user requests EPUB or PDF. Use the default source
unless the user names arXiv or provides an arXiv identifier. Never retry a
failed write automatically; report the job error and let the user decide.

Never reveal or repeat the API token, connector capability URL, or internal
server details.

For one-time phone setup or credential rotation, read
[`references/phone-setup.md`](references/phone-setup.md). The reusable
assistant instructions are in
[`references/assistant-instructions.md`](references/assistant-instructions.md).

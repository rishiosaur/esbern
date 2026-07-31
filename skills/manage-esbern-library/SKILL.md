---
name: manage-esbern-library
description: Search, browse, add books to, and synchronize the owner's hosted Esbern library from ChatGPT or Claude, including checking background jobs. Use when the user asks what books are in their library, whether they already own a title, asks to add, fetch, download, or install a book, or asks to sync or reconcile the library with reMarkable.
---

# Manage Esbern Library

Use the hosted Esbern tools exposed by the assistant. Do not run a local CLI or
read local folders.

## Choose the operation

- To see everything, call `get_full_library` (Claude) or `listLibrary`
  (ChatGPT).
- To find a book, call `search_library` or `searchLibrary`. Search by the most
  distinctive available title and author terms.
- Before every add, determine the requested edition's ISBN-13 from an ISBN the
  user supplied or a reliable metadata/web lookup. Never guess an ISBN. Search
  the library by ISBN first, then search the exact title, author, and edition
  because older catalog entries may not expose ISBN metadata.
- If the ISBN matches, or the title-author-edition search is a strong match,
  stop without calling the add tool. Respond: `Duplicate book error: this book
  is already in your library. Nothing was added.` Include the matching title,
  ISBN when known, and library path.
- If the requested edition or title is ambiguous, ask one short clarifying
  question before adding it.
- Only call `add_book` or `queueBook` after the user explicitly asks to add,
  fetch, download, or install the book. This starts an external download and a
  reMarkable sync.
- In Claude, keep `add_book` open and surface its live download, metadata, and
  reMarkable sync updates. Report its terminal result. If the stream is
  interrupted, use the job ID from the progress messages with `check_job`;
  the server-side job continues.
- In ChatGPT, report the job ID returned by `queueBook`. Use `getJob` when
  the user asks for progress, or once after enqueueing when the tool can be
  called without delaying the response.
- Only call `sync_library` (Claude) or `queueLibrarySync` (ChatGPT) when the
  user explicitly asks to synchronize or reconcile the library with
  reMarkable. Claude streams the sync phases through its open tool call.
  ChatGPT returns a job ID to check with `getJob`.

Use `auto` format unless the user requests EPUB or PDF. Use the default source
unless the user names arXiv or provides an arXiv identifier. Never retry a
failed write automatically; report the job error and let the user decide.

Never reveal or repeat the API token, connector capability URL, or internal
server details.

For one-time phone setup or credential rotation, read
[`references/phone-setup.md`](references/phone-setup.md). The reusable
assistant instructions are in
[`references/assistant-instructions.md`](references/assistant-instructions.md).

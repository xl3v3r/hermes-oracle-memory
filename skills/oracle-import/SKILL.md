---
name: oracle-import
description: Use when importing chat archives into Oracle memory.
version: 1.1.1.5
author: Hermes Community
metadata:
  hermes:
    tags: [database, memory, oracle, import, sharegpt]
---

# Oracle 26ai Conversation & Document Import

Import conversation archives (ShareGPT, ChatGPT and Claude exports) and large documents into the Oracle memory table without passing their text through the model.

## When to Use

- Importing historical conversations (`conversations.json`, ShareGPT files, export `.zip` files).
- Storing a large text file, document or log in Oracle memory.
- Any import too large to send as tool arguments.

## Prerequisites

- The Oracle memory provider is installed and configured (`OCI_DB_USER`, `OCI_DB_PASSWORD`, `OCI_DB_DSN`, and a wallet if the database needs one).
- The embedding model is loaded in the database (the provider cannot store rows without it).
- For the script: the `OCI_*` settings in the environment, or a file passed with `--env-file`. The script reads no file on its own.

## How to Run

### Option 1: one file, from a Hermes session

Call `oracle_chunk` with `file_path`. The tool reads the file on the Hermes host, recognises conversation JSON, and chunks it in the database:

```json
{"file_path": "~/exports/conversations.json"}
```

### Option 2: a large archive, from a terminal

`scripts/import_conversations_ordered.py` is in the plugin directory (`plugins/oracle` under the Hermes home). It connects to the database directly and uses no model tokens:

```bash
H="${HERMES_HOME:-$HOME/.hermes}"
cd "$H/plugins/oracle"
python3 scripts/import_conversations_ordered.py ~/exports/conversations.json --env-file "$H/.env"

# A zip export, under a session id of your choice
python3 scripts/import_conversations_ordered.py ~/exports/export.zip --session-id archive-2026 --env-file "$H/.env"
```

| Option | Effect |
|---|---|
| `--session-id <id>` | Session id for every imported row. Default: `import-ordered-<date>`. |
| `--max-words <n>` | Chunk size in words (default 80). |
| `--overlap <n>` | Words shared by neighbouring chunks (default 10). |
| `--limit-conversations <n>` | Import only the first `n` conversations, to try the import out. |
| `--env-file <path>` | Read `KEY=VALUE` settings from a file. The environment wins. |

## What Is Stored

- Each message is chunked on its own. Every chunk is a row with role `chunk` and target `memory`, so `oracle_search` and automatic recall return it whoever wrote the message.
- The speaker is kept in `metadata.sender`. A conversation's title and summary are stored as its first turn. In a summary export (a `conversations` list of `system` entries) every entry is its own document.
- Each chunk carries `conv_ref`, `turn_index`, `chunk_index` and `turn_id`, so a conversation can be read back in order.
- All rows of one run share one session id. `oracle_delete(session_id="<id>", dry_run=true)` counts them, and the same call without `dry_run` (and with the `max_rows` it names) removes the import.

## Supported Formats

| Format | Structure |
|---|---|
| ShareGPT | `{"conversations": [{"from": "...", "value": "..."}]}`, or a list of such sessions |
| Claude / ChatGPT export | `[{"name": "...", "summary": "...", "chat_messages": [...]}]` |
| Zip export | A `.zip` that contains the JSON (the largest `.json` in it is read) |
| Truncated JSON | The complete leading objects are imported |
| Plain text (`.txt`, `.md`, logs) | Use `oracle_chunk(file_path=...)` |

## Pitfalls

- **Secret paths are refused.** `oracle_chunk(file_path=...)` rejects credential and system paths (env files, `auth.json`, SSH, cloud and GPG key directories, `*.pem`, `*.key`, wallets, `/etc`, `/proc`), because chunks are stored in the database. Export or redact first.
- **Pass a path, not the text.** Do not paste file contents into a message or into `oracle_chunk(content=...)`.
- **An imported archive is recalled like any other memory.** Give the import its own `--session-id` so it can be found and removed as a unit.
- **`DPY-6005: cannot connect`.** The wallet is missing or unreadable, or the DSN does not match `tnsnames.ora`.
- **`ORA-40284: model does not exist`.** The embedding model is not loaded in this schema. See INSTALL.md.

## Verification

Search for a phrase from the archive:

```text
oracle_search(query="<a phrase from an imported conversation>", session_id="<the import's session id>")
```

The result should include rows with role `chunk` and the `conv_ref` of that conversation.

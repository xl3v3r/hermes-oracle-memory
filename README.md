# Hermes Agent Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.4-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

## About

`hermes-oracle-memory` is a high-performance external memory provider plugin for [Hermes Agent](https://github.com/nousresearch/hermes-agent), leveraging Oracle Autonomous AI Database (ADB) 26ai. It provides hybrid vector and full-text memory retrieval across conversations, enabling low-latency, scalable, and durable long-term memory for AI agents without relying on local SQLite or legacy memory systems like Mnemosyne.

## What's New in v1.0.4

- **`oracle-import` Hermes Skill**: Import conversation history archives (ShareGPT, Claude, ChatGPT, and overview JSON files) and large documents directly into Oracle 26ai ADB memory without exhausting LLM context windows or triggering API rate limits (e.g. HTTP 429).
- **Direct File Chunking via `oracle_chunk` (`file_path`)**: The `oracle_chunk` tool now supports reading files directly from disk via `file_path`, running in-database chunking with `DBMS_VECTOR.UTL_TO_CHUNKS` and avoiding multi-megabyte prompt token inflation.
- **Multi-Format & Truncated JSON Extraction**: Native turn extraction across ShareGPT, Claude, and overview structures with automatic recovery of complete leading JSON objects in truncated files.
- **ADB Wallet Auto-Detection**: Automatic discovery of Oracle ADB mTLS wallet credentials in `~/.oci/wallet` and `~/.hermes/oracle_wallet` across thin and thick modes.

## Configuration Reference

### 1. OCI CLI & SDK Configuration (`~/.oci/config`)

Required for Oracle Cloud Infrastructure API authentication, OCI CLI tools, and OCI Generative AI inference.

```ini
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaaexampleuserocid
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
tenancy=ocid1.tenancy.oc1..aaaaaaaaexampletenancyocid
region=us-ashburn-1
key_file=~/.oci/oci_api_key.pem
pass_phrase=YourKeyPassphraseHere
```

| Field | Requirement | Description |
|-------|-------------|-------------|
| `user` | **Required** | OCID of your OCI IAM user. |
| `fingerprint` | **Required** | Fingerprint of the public RSA key uploaded to the OCI Console under API Keys. |
| `tenancy` | **Required** | OCID of your tenancy. |
| `region` | **Required** | OCI region identifier (e.g. `us-ashburn-1`). |
| `key_file` | **Required** | Absolute path or `~` path to the private RSA signing key (`.pem`). |
| `pass_phrase` | **Required if key is encrypted** | Passphrase for the encrypted private PEM key. <br>**Critical:** If the `.pem` key was generated with a passphrase and `pass_phrase` is omitted, the OCI SDK will prompt on `stdin` (`Enter PEM Passphrase:`), hanging non-interactive agent gateways and daemon services. |

---

### 2. Hermes Environment Variables (`~/.hermes/.env`)

Configure database credentials, mTLS wallet authentication, and embedding providers in `$HERMES_HOME/.env` (default: `~/.hermes/.env`).

#### Database Connection (Required)

| Variable | Requirement | Description |
|----------|-------------|-------------|
| `OCI_DB_USER` | **Required** | Database username (defaults to `ADMIN` if omitted). |
| `OCI_DB_PASSWORD` | **Required** | Password for the database user on Oracle ADB 26ai. |
| `OCI_DB_DSN` | **Required** | Oracle Net Services connection descriptor or TNS alias (e.g. `(DESCRIPTION=(RETRY_COUNT=20)(RETRY_DELAY=3)(ADDRESS=(PROTOCOL=tcps)(PORT=1521)(HOST=...))(CONNECT_DATA=(SERVICE_NAME=...))(SECURITY=(SSL_SERVER_DN_MATCH=yes)))`). |

#### Wallet & Passphrase Settings

| Variable | Requirement | Description |
|----------|-------------|-------------|
| `OCI_WALLET_LOCATION` | Optional / Recommended | Directory path containing extracted mTLS client wallet files (`cwallet.sso`, `tnsnames.ora`, etc.). If omitted, auto-detected from `~/.oci/wallet` and `~/.hermes/oracle_wallet`, or uses `TNS_ADMIN`. |
| `OCI_WALLET_PASSWORD` | Optional | Password for encrypted wallet containers (such as `ewallet.p12` / `ewallet.pem` when not using auto-login `cwallet.sso`). |
| `OCI_CLI_PASSPHRASE` | Optional | Passphrase for encrypted `oci_api_key.pem` when running CLI invocations. |
| `OCI_KEY_PASSPHRASE` | Optional | Passphrase for OCI SDK API key if provided via environment variables. |

#### Embeddings (At least one provider required)

Memories require 1536-dimensional FLOAT64 embeddings matching `HERMES_AGENT_MEMORY.EMBEDDING`:

| Variable | Requirement | Description |
|----------|-------------|-------------|
| `OPENROUTER_API_KEY` | Recommended / Fallback | API key for OpenRouter to embed using `openai/text-embedding-3-small` (1536-d). |
| `OCI_GENAI_REGION` | Optional | OCI region for OCI Generative AI inference (e.g., `us-chicago-1`, `us-ashburn-1`). Uses Instance Principal authentication. |
| `OCI_COMPARTMENT_ID` | Optional | Compartment OCID where OCI Generative AI inference is permitted. |
| `OCI_EMBED_MODEL` | Optional | Embedding model ID on OCI GenAI (defaults to `cohere.embed-v4.0`). |

#### Table & Model Customizations (Optional)

| Variable | Requirement | Description |
|----------|-------------|-------------|
| `HERMES_ORACLE_TABLE` | Optional | Custom table name for memory storage (defaults to `HERMES_AGENT_MEMORY`). |
| `HERMES_ORACLE_ONNX_MODEL`| Optional | Local path to an in-database ONNX model if using native Oracle DB ONNX embeddings. |
| `MEMORY_RETENTION_DAYS` | Optional | Number of days to retain active agent memories before auto-cleanup. |

#### Example `~/.hermes/.env` Snippet

```bash
# Oracle ADB 26ai Connection
OCI_DB_USER=ADMIN
OCI_DB_PASSWORD="YourDatabasePasswordHere"
OCI_DB_DSN="(DESCRIPTION=(RETRY_COUNT=20)(RETRY_DELAY=3)(ADDRESS=(PROTOCOL=tcps)(PORT=1521)(HOST=adb.us-ashburn-1.oraclecloud.com))(CONNECT_DATA=(SERVICE_NAME=xxxx_high.adb.oraclecloud.com))(SECURITY=(SSL_SERVER_DN_MATCH=yes)))"

# Wallet & Passphrases
OCI_WALLET_LOCATION=~/.oci/wallet
OCI_WALLET_PASSWORD="YourPassphrase"
OCI_CLI_PASSPHRASE="YourPassphrase"
OCI_KEY_PASSPHRASE="YourPassphrase"

# Embeddings
OPENROUTER_API_KEY="sk-or-v1-..."
```

---

## Setup

```bash
hermes config set memory.provider oracle
```

Then restart the gateway / start a new session.

## What it uses on ADB

- `VECTOR(1536, FLOAT64)` + IVF neighbor-partition index (`HERMES_AGENT_MEMORY_VEC_IDX`)
- Oracle Text `SEARCH INDEX` on `CONTENT` (`CONTAINS` / `SCORE`)
- Hybrid recall: VECTOR + Text `UNION ALL` in one SQL round-trip (no native HVI on this ADB)
- Writes insert text immediately; embeddings backfill in batches of 32
- Query embedding LRU cache (256)
- Prefetch wait 0.5s
- `DBMS_VECTOR.UTL_TO_CHUNKS` for long-document and file splits
- Hybrid rank: 0.7 * (1 - cosine distance) + 0.3 * normalized text score

## Tools

| Tool | Description |
|------|-------------|
| `oracle_search` | hybrid / vector / text recall |
| `oracle_add` | store a durable fact (embedded + indexed) |
| `oracle_update` | replace content by memory_id |
| `oracle_delete` | delete by memory_id |
| `oracle_chunk` | split text or disk file (`content` or `file_path`, supporting ShareGPT/JSON exports) with in-database UTL_TO_CHUNKS |

### Enhanced `oracle_chunk` Tool

The `oracle_chunk` tool supports direct file chunking via `file_path` in addition to inline `content`. When `file_path` is provided, the tool reads the file locally, automatically parses supported conversation export formats (ShareGPT, Claude, ChatGPT, or summary JSON) into structured turns, and executes in-database chunking using Oracle ADB's `DBMS_VECTOR.UTL_TO_CHUNKS`. This avoids piping large file payloads through the LLM prompt context.

**Parameters:**
- `file_path` *(string, optional)*: Absolute or relative path to a local text, markdown, or JSON file.
- `content` *(string, optional)*: Inline text content to chunk.
- `max_words` *(integer, default 80)*: Maximum words per chunk.
- `overlap` *(integer, default 10)*: Number of overlapping words between consecutive chunks.

**Tool Call Example:**
```json
{
  "file_path": "/path/to/conversations.json",
  "max_words": 1000,
  "overlap": 10
}
```

## Skills

### `oracle-import`

Located at [`skills/oracle-import/SKILL.md`](skills/oracle-import/SKILL.md), this skill enables bulk ingestion of historical conversation archives and large documents into Oracle Autonomous AI Database 26ai memory without hitting LLM token limits or upstream provider rate limits (such as NVIDIA NIM HTTP 429).

#### Supported Formats

| Format | Structure | Parsing Behavior |
|--------|-----------|------------------|
| **ShareGPT** | `{"conversations": [{"from": "...", "value": "..."}]}` | Turns extracted and prefixed with `[ROLE]` |
| **Claude Export** | `[{"name": "...", "summary": "...", "chat_messages": [...]}]` | Extracts titles, summaries, and user/assistant messages |
| **ChatGPT Export** | Standard ChatGPT multi-turn archive | Formats user and assistant turns with message roles |
| **Summary / Overview JSON** | `{"conversations": [...]}` or conversation summaries | Ingested as system/session memory turns |
| **Plain Text / Markdown** | `.txt`, `.md`, logs | Split recursively via in-database `UTL_TO_CHUNKS` |
| **Truncated JSON Streams** | Partial or aborted downloads | Recovers valid leading JSON objects without failing |

#### Ingestion Methods

1. **Via Hermes Agent (`oracle_chunk`)**:
   Agents can directly invoke the `oracle_chunk` tool passing `file_path`:
   ```bash
   oracle_chunk(file_path="/path/to/archive.json", max_words=500, overlap=20)
   ```

2. **Standalone Bulk Ingestion Script**:
   For large exports (>10MB or hundreds of sessions), run the bundled CLI script to import batches directly into Oracle ADB with zero LLM API consumption:
   ```bash
   # Import an archive
   python3 skills/oracle-import/scripts/import_conversations.py /path/to/oracle_import.json

   # Specify custom batch size and session ID
   python3 skills/oracle-import/scripts/import_conversations.py /path/to/conversations.json --batch-size 50 --session-id "archive-2026-06"
   ```


## Contributing

Contributions to `hermes-oracle-memory` are welcome!

To protect the project and ensure clean licensing for all users and contributors, this repository uses **[CLA Assistant](https://cla-assistant.io/)**. When opening a pull request for the first time, the automated bot will request that you agree to the [Contributor License Agreement (CLA)](./CLA.md). You can sign it directly by commenting:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

This project is licensed under the **Business Source License 1.1 (BSL 1.1)**. See the [LICENSE](./LICENSE) file for complete terms.

* **Free for Personal, Research, and Testing Use**: Anyone may use, study, and modify the code.
* **Free for Organizations with < $1M Annual Revenue**: Small teams and early-stage startups may use the software in production free of charge.
* **Commercial License Required**: Use within organizations with annual gross revenue exceeding $1,000,000 USD or offering the software as a commercial managed service requires a commercial license agreement. Contact Taylor Ripley (`xl3v3r`) for commercial licensing inquiries.
* **Automatic Open Source Conversion**: On October 11, 2030 (the Change Date), the license automatically converts to the **Apache License, Version 2.0**.

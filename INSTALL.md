# Hermes Agent — Oracle 26ai Memory Provider · Installation Guide

![Version](https://img.shields.io/badge/version-1.0.6-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

Step-by-step setup for the `oracle` memory provider.

## Prerequisites

- **Python 3.10+**
- **Oracle Autonomous AI Database 26ai** (ADB-S or ADB-D) with a database user (e.g. `ADMIN`) able to create tables, indexes, and ONNX mining models
- **Hermes Agent** installed (`hermes` on your `PATH`)
- mTLS wallet (if using mutual TLS) extracted to a local directory

## 1. Install the provider

### Via the plugin manager (recommended)

```bash
hermes plugins install xl3v3r/hermes-oracle-memory
```

`hermes plugins install` also accepts a full Git URL:
`hermes plugins install https://github.com/xl3v3r/hermes-oracle-memory`.

### Or clone manually

```bash
git clone https://github.com/xl3v3r/hermes-oracle-memory.git ~/.hermes/plugins/oracle
pip install "oracledb>=4.0.2"

# Optional — only required for the OCI GenAI embedding fallback:
pip install oci
```

## 2. Select the provider

```bash
hermes memory setup
```

Choose **`oracle`**. This is equivalent to setting `memory.provider: oracle` in
`~/.hermes/config.yaml`. Restart Hermes (or start a new session) so the
provider initializes.

## 3. Configure credentials (`~/.hermes/.env`)

Secrets belong in `~/.hermes/.env`, never in `config.yaml`.

```bash
# ── Database (required) ─────────────────────────────────────────────
OCI_DB_USER=ADMIN
OCI_DB_PASSWORD="YourDatabasePasswordHere"
OCI_DB_DSN="(DESCRIPTION=(RETRY_COUNT=20)(RETRY_DELAY=3)(ADDRESS=(PROTOCOL=tcps)(PORT=1521)(HOST=adb.us-ashburn-1.oraclecloud.com))(CONNECT_DATA=(SERVICE_NAME=xxxx_high.adb.oraclecloud.com))(SECURITY=(SSL_SERVER_DN_MATCH=yes)))"

# ── mTLS wallet (required if using mutual TLS) ──────────────────────
OCI_WALLET_LOCATION=~/.oci/wallet
OCI_WALLET_PASSWORD="YourWalletPassword"

# ── Embedding providers (at least one) ──────────────────────────────
HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5   # in-database ONNX (primary, zero API cost)
OPENROUTER_API_KEY="sk-or-v1-..."           # fallback

# OCI GenAI fallback (optional, instance-principal auth — requires `pip install oci`)
OCI_GENAI_REGION=us-chicago-1
OCI_COMPARTMENT_ID=ocid1.compartment.oc1..aaaaaaaexample
```

## 4. (Optional) Install the in-database ONNX model

```bash
python3 scripts/setup_onnx_model.py
echo 'HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5' >> ~/.hermes/.env
```

This loads `BAAI/bge-base-en-v1.5` (768-d) into the ADB kernel so embeddings
are computed in SQL with no external API cost or latency.

## 5. Verify

```bash
hermes memory status               # confirm "oracle" is active
python3 scripts/verify_provider.py # connectivity + embedding + recall
pytest tests/                      # offline unit tests
```

End-to-end smoke test:

```bash
hermes chat -q "Remember my preferred deployment target is Oracle Cloud OCI."
hermes chat -q "What is my preferred deployment target?"
```

## 6. Upgrade / backfill legacy embeddings

```bash
python3 scripts/backfill_embeddings.py --workers 4 --batch-size 32
```

Resumes from `backfills/backfill_progress_768.json` and refreshes vector
indexes and table statistics on completion.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Provider not active after install | Restart Hermes; confirm `hermes memory status`. |
| `OCI_DB_PASSWORD` / `OCI_DB_DSN` missing | Set both in `~/.hermes/.env`; the provider reports unavailable without them. |
| OCI GenAI fallback raises `ImportError` | `pip install oci`. |
| Hangs on `Enter PEM Passphrase` | Set `pass_phrase` in `~/.oci/config`, or `OCI_KEY_PASSPHRASE` in `.env`. |
| Embeddings sent to a third party | Set `HERMES_ORACLE_DISABLE_EXTERNAL_EMBED=1` to forbid the OpenRouter/OCI fallback. |

## License

[BSL 1.1](LICENSE). See the [README](README.md) for details.

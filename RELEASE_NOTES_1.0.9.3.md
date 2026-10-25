# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.3

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.3 updates are CRITICAL to functionality. Caps individual `oracle_search` query timeout at 13 seconds with clean fallback and fail-fast error handling, synchronizes with Hermes Agent `timeouts.tools.concurrent_batch` 60s in `~/.hermes/config.yaml`, decouples query timeout errors from ONNX health monitoring, and enforces zero drift across repository and local plugin environments.

### What's New in v1.0.9.3

#### ⚡ 13-Second Individual Query Timeout Cap & Fail-Fast Execution
- **Bound Round-Trip Database Latency**: Individual `oracle_search` queries are capped at 13.0 seconds (`_SEARCH_TIMEOUT_SECS = 13.0`, `call_timeout = 13000` ms) via Oracle `SessionPool` and connection settings.
- **Configurable Environment Override**: Defaults to 13.0 seconds, configurable via `HERMES_ORACLE_SEARCH_TIMEOUT`.
- **Driver-Level Cancellation**: At the `python-oracledb` socket/driver layer, slow or blocked round trips abort automatically after 13s, preventing worker threads from hanging indefinitely.

#### 🛡️ Clean Fallback & Fail-Fast Error Handling
- **Graceful Timeout Interception**: When `oracle_search` queries encounter database call timeouts (`DPI-1067`, `ORA-01013`, `DPY-4011`, `DPY-4024`, `TimeoutError`), the provider cleanly catches the timeout.
- **Coherent Model Feedback**: Returns a clean JSON response (`{"result": "No relevant memories found (oracle_search query timed out after 13s).", "count": 0, "timed_out": True}`) rather than bubbling up as an unhandled exception or breaking agent turn flow.
- **Decoupled from ONNX Health**: Query timeouts are user/network latency events rather than ONNX engine crashes. They do not trigger `self._onnx_mark_failed()` or cause spurious 60-second ONNX cooldown lockouts.

#### ⏱️ Hermes Agent Concurrent Batch Timeout Alignment (60s)
- **Batch Deadlock Elimination**: Synchronized with Hermes Agent `timeouts.tools.concurrent_batch: 60` in `~/.hermes/config.yaml`.
- **Guaranteed Completion Window**: Because individual queries fail fast within 13 seconds, parallel tool execution batches comfortably finish well inside the 60s batch deadline, completely eliminating batch hangs (such as observed in conversation `20261024_200633_adba7e`).

#### 🔒 Zero Drift & Core File Integrity
- **Core Repository Isolation**: Zero modifications made to Hermes Agent core repository files (`~/.hermes/hermes-agent`), keeping upstream mainstream branches cleanly intact.
- **100% Repository & Plugin Parity**: Strict bidirectional byte-level synchronization maintained between the repository (`/home/rocky/hermes-oracle-memory`) and the active plugin directory (`~/.hermes/plugins/oracle`).

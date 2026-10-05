# Signalpost - Submission Runbook

**Signalpost** is an open-source agent system designed to research, verify, and maintain public information about Norwegian companies using official public data sources.

---

## 1. Quick Start & Installation

### Requirements
- **Python 3.9+**

### Installation Command
```bash
pip install -r requirements.txt
```

---

## 2. One-Command Evaluator Execution

Signalpost provides an evaluator-facing runner that processes single or multiple organization numbers, enforces outbound request budgets, isolates per-company errors, and outputs structured JSON.

### Run Command
```bash
python -m signal_post --run input.json --output results.json --db signalpost.db --request-budget 150
```

### Stdin / Stdout Execution
```bash
cat input.json | python -m signal_post --run - --output - --db signalpost.db --request-budget 150
```

### Options
- `--run INPUT_FILE` or `-r INPUT_FILE`: Input JSON file containing organization numbers (or `-` for stdin).
- `--output OUTPUT_FILE` or `-o OUTPUT_FILE`: Output JSON file path (or `-` for stdout).
- `--db DB_FILE`: Target SQLite database file (default: `signalpost.db`).
- `--no-live` / `--offline`: Disable live HTTP requests and serve profiles from local SQLite cache.
- `--request-budget INT`: Set maximum outbound HTTP request budget (default: `2000`).

---

## 3. Input & Output Schemas

### Input Format
Accepts a JSON array of 9-digit Norwegian organization numbers:
```json
[
  "923609016",
  "974760673",
  "810034882"
]
```
Or a JSON object:
```json
{
  "org_numbers": [
    "923609016",
    "974760673"
  ]
}
```

### Output Format
The evaluator runner produces structured JSON with execution metrics and company facts.
The `status` field precisely distinguishes profile resolution behavior:
- `live_refreshed`: Successfully retrieved and updated via live REST API.
- `served_from_local`: Served directly from local database (offline mode or pre-existing profile).
- `local_fallback_after_refresh_failure`: Served from local database cache after a live network refresh attempt failed or request budget was exhausted.
- `failed`: Unable to resolve profile (invalid org number, non-existent, or network failure without local fallback).

```json
{
  "run_metrics": {
    "requested_count": 3,
    "processed_count": 3,
    "success_count": 3,
    "failed_count": 0,
    "served_from_local_count": 0,
    "local_fallback_count": 0,
    "live_refreshed_count": 3,
    "total_outbound_requests": 3,
    "elapsed_seconds": 0.85,
    "average_seconds_per_company": 0.2833,
    "estimated_external_api_cost": "$0"
  },
  "results": [
    {
      "org_number": "923609016",
      "name": "EQUINOR ASA",
      "status": "live_refreshed",
      "facts": [
        {
          "fact_key": "name",
          "value": "EQUINOR ASA",
          "source_name": "Brønnøysund Register Centre - Enhetsregisteret",
          "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
          "retrieved_at": "2026-10-05T15:30:00+00:00",
          "source_validity_date": null,
          "verification_status": "source_asserted"
        }
      ],
      "source_urls": [
        "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
      ],
      "retrieval_dates": [
        "2026-10-05T15:30:00+00:00"
      ],
      "changes": [],
      "warnings": [],
      "errors": [],
      "summary": "Company 'EQUINOR ASA' (923609016): 20 active source-supported facts. Refreshed via official registry REST API."
    }
  ]
}
```

---

## 4. Database Architecture & Storage Behavior

Signalpost uses SQLite with relational tables and strict transaction safety:
- **`companies`**: Primary records keyed by 9-digit `org_number`.
- **`facts`**: Granular key-value assertions indexed via a partial unique index `(org_number, fact_key) WHERE is_active = 1`.
- **`evidence`**: Lineage links containing exact source URLs, retrieval timestamps, validity dates, and raw open-data payloads (`raw_evidence`).
- **`change_history`**: Audit trail recording `previous_value` -> `new_value`, change timestamps, and change explanations (`created`, `updated`, `reasserted`, `conflict`).
- **`request_log`**: Outbound HTTP request tracker enforcing daily request budget limits.

---

## 5. API, Model & Financial Cost Details

- **Model Usage**: **$0** (Pure Python code performs deterministic extraction, validation, and storage; zero external LLM inference or subscription costs).
- **Public API**: Brønnøysund Register Centre REST API (`https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}`).
- **Authentication**: None required (Free public open data under NLOD).
- **Estimated Financial Cost**: **`$0.00`** ($0 external API subscription fees and $0 AI model inference fees).

---

## 6. Reproducing the 1,000-Profile Bootstrap

To reproduce the deterministic 1,000-profile bootstrap from the local bulk dataset (`enheter_alle_test.json.gz`) with **0 network requests**:

1. **Generate Selection Manifest**:
   ```bash
   python -m signal_post --generate-manifest --file enheter_alle_test.json.gz --limit 1000
   ```
2. **Import Profiles from Manifest (0 HTTP Requests)**:
   ```bash
   python -m signal_post --import-manifest data/bootstrap_manifest.json --file enheter_alle_test.json.gz --db signalpost_1000.db
   ```
3. **Validate Database Integrity**:
   ```bash
   python -m signal_post --validate-db --db signalpost_1000.db --limit 1000
   ```

---

## 7. Running Unit Tests

Run the complete offline test suite (81 passing tests):
```bash
python -m unittest discover -s tests
```

---

## 8. Canonical Live 100-Company Benchmark Results (Phase 6.2 Provenance Freeze)

The canonical live benchmark evaluated 100 organization numbers directly against the official Brønnøysund REST API without post-run manual edits:

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output benchmark_canonical_results.json --db benchmark_canonical.db --request-budget 150
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `benchmark_canonical_results.json` (SHA-256: `10a210904cd511c922cdcadb79936064da61c5ebde3a87f82e79c4db4db453e1`)
- **Database File**: `benchmark_canonical.db`
- **Metadata Provenance File**: `benchmark_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Identity Matches**: `100 / 100` (`100.0%` match rate)
- **Run-Level HTTP Request Delta**: `100` (exactly 1 request per company during run)
- **Wall-Clock Runtime**: **`26.05 seconds`** (Avg `0.2605s` / company)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Facts Stored**: `1,907` active facts (Avg `19.07` facts / company; Min: `15`, Max: `25`)
- **Evidence Linkage Rate**: **`100.0%`** (1,907 / 1,907 facts linked to complete evidence lineage)

*Artifact Provenance Note:*
The initial `benchmark_results_100.json` and `benchmark_100.db` files are retained strictly as intermediate development artifacts. The canonical, immutable submission benchmark is `benchmark_canonical_results.json` / `benchmark_canonical.db` backed by `benchmark_metadata.json`.

### Evaluation Against Hackathon Limits
- **Time Limit (45 Minutes)**: `26.05 seconds` is **99.0% below** the 45-minute limit.
- **Request Limit (2,000 Requests)**: `100 requests` is **95.0% below** the 2,000-request limit.

---

## 9. Known Limitations

- **Single Primary Source**: Integrated strictly with official Brønnøysund `Enhetsregisteret`. Additional sources (e.g. `Underenheter`, financial statements) can be added in future stages.
- **Synchronous Execution**: REST API requests execute sequentially per company to maintain strict request budget control.

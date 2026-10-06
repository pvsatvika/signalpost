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

Run the complete offline test suite (119 passing tests):
```bash
python -m unittest discover -s tests
```

---

## 8. Canonical Live 100-Company Baseline Benchmark Results (Phase 6.2 Provenance Freeze)

The canonical live baseline benchmark evaluated 100 organization numbers directly against the official Brønnøysund REST API without post-run manual edits:

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

---

## 9. Live 100-Company Roles API Coverage Expansion Benchmark (Phase 7)

The Phase 7 coverage expansion benchmark evaluated the same 100 organization numbers with dual-source enrichment (**Enhetsregisteret + Roles API**):

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output coverage_roles_results_100.json --db coverage_roles_100.db --request-budget 250
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `coverage_roles_results_100.json` (SHA-256: `e45559698e45cea26a0829145d9e19f4a0e0c9be8403e0b17c45567539f925df`)
- **Database File**: `coverage_roles_100.db`
- **Metadata Provenance File**: `coverage_roles_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Identity Matches**: `100 / 100` (`100.0%` match rate)
- **Outbound HTTP Request Delta**: `200` (exactly 2 requests per company: 1 base + 1 roles)
- **Wall-Clock Runtime**: **`54.77 seconds`** (Avg `0.5477s` / company)
- **Companies Enriched with Roles**: `67 / 100` (`67.0%` of benchmark companies have registered role groups)
- **Total Active Facts Stored**: `2,345` active facts (**+438 new active role facts**, Avg `23.45` facts / company)
- **Average Role Facts Among Enriched Companies**: `6.54` role facts / company
- **Privacy Compliance**: `100%` (0 birth dates `fodselsdato` and 0 national identity numbers `fnr` exposed)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Evidence Linkage Rate**: **`100.0%`** (2,345 / 2,345 facts linked to complete evidence lineage)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)

---

## 10. Live 100-Company Finanstilsynet Exploratory Benchmark (Phase 9)

The Phase 9 exploratory benchmark evaluated the same 100 organization numbers with triple-source enrichment (**Enhetsregisteret + Roles API + Finanstilsynet Virksomhetsregisteret v2**):

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output coverage_finanstilsynet_results_100.json --db coverage_finanstilsynet_100.db --sources registry,roles,finanstilsynet --request-budget 350
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `coverage_finanstilsynet_results_100.json` (SHA-256: `04bb7aeabff5fa4d3be6d6ef38fb9a8ddbd77fcf50c3f59fa07f43399fcab9ae`)
- **Database File**: `coverage_finanstilsynet_100.db`
- **Metadata Provenance File**: `coverage_finanstilsynet_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Outbound HTTP Request Breakdown**: `300` total requests (`100` base + `100` roles + `100` finanstilsynet)
- **Wall-Clock Runtime**: **`94.87 seconds`** (Avg `0.9487s` / company)
- **Companies Matched in Finanstilsynet**: `1 / 100` (`1.0%` of general diversified company sample)
- **Regulatory Facts Added**: `6` active facts (`finanstilsynet_id` + 5 active licences for `810359862 AUTOBJØRN A/S`)
- **Total Combined Active Facts Stored**: `2,351` active facts
- **Privacy Compliance**: `100%` (0 birth dates, 0 national identity numbers, 0 residential addresses exposed)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Evidence Linkage Rate**: **`100.0%`** (2,351 / 2,351 facts linked to complete evidence lineage)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)

---

## 11. Fresh Canonical 100-Company Financial Benchmark (Phase 10.2)

The Phase 10.2 canonical benchmark evaluated the 100 organization numbers on a fresh database (**Enhetsregisteret + Roles API + Regnskapsregisteret**):

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output benchmark_accounts_canonical_results.json --db benchmark_accounts_canonical.db --sources registry,roles,accounts --request-budget 400
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `benchmark_accounts_canonical_results.json` (SHA-256: `5c395e341dbf28a43f44bbbfd580c47cbf57c40982783d38342b08bd543d0258`)
- **Database File**: `benchmark_accounts_canonical.db`
- **Metadata Provenance File**: `benchmark_accounts_canonical_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Outbound HTTP Request Breakdown**: `300` total requests (`100` base + `100` roles + `100` accounts)
- **Wall-Clock Runtime**: **`71.94 seconds`** (Avg `0.7194s` / company)
- **Companies Enriched with Financial Key Figures**: `68 / 100` (`68.0%` of benchmark sample have filed accounts)
- **Missing-Currency Withholds**: `0` (all 68 enriched companies provided explicit `valuta` metadata)
- **Conflicting Duplicate Withholds**: `0` (0 conflicting duplicate filings found in this sample)
- **Total Financial Facts Published**: `2,178` active financial facts (Avg `32.03` financial facts / enriched company)
- **Total Combined Active Facts Stored**: `4,523` active facts (`1,907` base + `438` roles + `2,178` accounts)
- **Accuracy & Whitelist Enforcement**: `100%` (strictly 11 whitelisted source fields; 0 derived ratios, 0 profit margins, 0 OCR/parsing errors)
- **Scope & Currency Precision**: `100%` (company `selskap` vs group `konsern` separated; `NOK`/`USD` preserved; zero and negative amounts preserved; exact source numeric amounts preserved without scaling or conversion)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Evidence Linkage Rate**: **`100.0%`** (4,523 / 4,523 facts linked to complete evidence lineage)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)
- **Default Source Strategy Decision**: Default sources updated to `--sources registry,roles,accounts`.

*(Note: The previous `coverage_accounts_100.db` database file represents a development coverage artifact resulting from an interrupted initial run; `benchmark_accounts_canonical.db` represents the clean submission canonical benchmark).*

---

## 12. Live 100-Company Fullmakttjenesten Benchmark (Phase 11)

The Phase 11 benchmark evaluated the 100 organization numbers with quadruple-source enrichment (**Enhetsregisteret + Roles API + Regnskapsregisteret + Fullmakttjenesten**):

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output coverage_fullmakt_results_100.json --db coverage_fullmakt_100.db --sources registry,roles,accounts,fullmakt --request-budget 1500
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `coverage_fullmakt_results_100.json` (SHA-256: `6ee4fff2218cac377c6284bc117936ea335f4ef7fcb18776137428254d3eb559`)
- **Database File**: `coverage_fullmakt_100.db`
- **Metadata Provenance File**: `coverage_fullmakt_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Outbound HTTP Request Breakdown**: `500` total requests (`100` base + `100` roles + `100` accounts + `100` signatur + `100` prokura)
- **Wall-Clock Runtime**: **`159.93 seconds`** (Avg `1.5993s` / company)
- **Companies Enriched with Authority Rules**: `48 / 100` (`48.0%` of benchmark sample have registered signature or procuration rules)
- **Total Authority Facts Published**: `73` active facts (`fullmakt_signatur_combination_...` and `fullmakt_prokura_combination_...`)
- **Total Combined Active Facts Stored**: `4,596` active facts (`1,907` base + `438` roles + `2,178` accounts + `73` fullmakt)
- **Privacy Compliance**: `100%` (0 birth dates `fodselsdato`, 0 national identity numbers `fnr`, 0 D-numbers exposed)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Evidence Linkage Rate**: **`100.0%`** (4,596 / 4,596 facts linked to complete evidence lineage)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)
- **Default Source Strategy Decision**: Default sources remain `--sources registry,roles,accounts` (300 requests, 71.94s, 4,523 facts). Fullmakttjenesten is provided as an optional source (`--sources registry,roles,accounts,fullmakt`) when binding authority rules are required.

---

## 13. Live 100-Company Underenheter Benchmark (Phase 12)

The Phase 12 benchmark evaluated the 100 organization numbers with quadruple-source enrichment (**Enhetsregisteret + Roles API + Regnskapsregisteret + Underenheter**):

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output coverage_subentities_results_100.json --db coverage_subentities_100.db --sources registry,roles,accounts,subentities --request-budget 1500
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `coverage_subentities_results_100.json` (SHA-256: `e58ae110019b14a12397859e0879a3ae8b9bf0b7c5aa8ff5e819c4ebaa2db8a0`)
- **Database File**: `coverage_subentities_100.db`
- **Metadata Provenance File**: `coverage_subentities_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Outbound HTTP Request Breakdown**: `400` total requests (`100` base + `100` roles + `100` accounts + `100` subentities)
- **Pagination Requests**: `0` additional pagination requests required for this sample (all fetched within single page)
- **Wall-Clock Runtime**: **`163.75 seconds`** (Avg `1.6375s` / company)
- **Companies Enriched with Operating Units**: `67 / 100` (`67.0%` of benchmark sample have registered operating units / branches)
- **Total Operating Units Discovered**: `71` underenheter
- **Total Subentity Facts Published**: `551` active child-scoped facts (`subentity_<child_org>_location_address`, `subentity_<child_org>_industry`, `subentity_<child_org>_employee_count`, etc.)
- **Total Combined Active Facts Stored**: `5,074` active facts (`1,907` base + `438` roles + `2,178` accounts + `551` subentities)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Evidence Linkage Rate**: **`100.0%`** (5,074 / 5,074 facts linked to complete evidence lineage)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)
- **Default Source Strategy Decision**: Default sources remain `--sources registry,roles,accounts` (300 requests, 71.94s, 4,523 facts). Underenheter is provided as an optional source (`--sources registry,roles,accounts,subentities`) when physical operating locations and branch activity details are needed.

---

## 14. Limitations & Scope

- **Data Source Scope**: Signalpost integrates official open APIs from Brønnøysund (`Enhetsregisteret`, `Roles API`, `Regnskapsregisteret`, `Fullmakttjenesten`, `Underenheter`) and Finanstilsynet (`Virksomhetsregisteret v2`).
- **Financial Statement Availability**: Statements are only available for entities required to submit annual accounts to Regnskapsregisteret (e.g. `AS`, `ASA`). Non-reporting entity forms (e.g. sole proprietorships `ENK`) return `success_empty` or `not_found` cleanly.
- **Authority Rule Machine Coverage**: Signature and procuration machine rules in Fullmakttjenesten are available for supported commercial forms (`AS`, `ENK`, `ANS`, etc.). Unmaintained forms (`ORGL`, `ADM`, etc.) return `unsupported_org_form` without failing.
- **Operating Unit Boundaries**: Subentity attributes (addresses, employee counts, activities) are strictly child-scoped and are never merged into parent legal entity attributes.
- **Currency & Scaling**: Signalpost preserves exact source-supplied numeric amounts and currencies (`valuta`) without amount scaling or currency conversion. Statements missing currency metadata are withheld from monetary fact publication.
- **Duplicate Resubmissions**: Identical duplicate filings are deduplicated; conflicting resubmissions for the same period/scope are withheld from clean publication to prevent published ambiguity.
- **Synchronous API Execution**: Outbound REST requests execute sequentially per company to maintain strict request budget control.

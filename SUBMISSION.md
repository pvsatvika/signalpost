# Signalpost - Submission Runbook

**Signalpost** is an open-source agent system designed to research, verify, and maintain public information about Norwegian companies using official public data sources.

---

## 1. Quick Start & Installation

### Requirements
- **Python 3.9+**

### Installation Command

#### Windows (PowerShell / Command Prompt)
```powershell
pip install -r requirements.txt
```

#### Linux / macOS
```bash
pip install -r requirements.txt
```

---

## 2. One-Command Evaluator Execution

Signalpost provides an evaluator-facing runner that processes single or multiple organization numbers, enforces outbound request budgets, isolates per-company errors, and outputs structured JSON.

### Run Command (General Purpose Competition Default)
```bash
python -m signal_post --run input.json --output results.json --db signalpost.db --sources registry,roles,accounts,subentities --request-budget 600
```

### Stdin / Stdout Execution
```bash
cat input.json | python -m signal_post --run - --output - --db signalpost.db --sources registry,roles,accounts,subentities --request-budget 600
```

### Options
- `--run INPUT_FILE` or `-r INPUT_FILE`: Input JSON file containing organization numbers (or `-` for stdin).
- `--output OUTPUT_FILE` or `-o OUTPUT_FILE`: Output JSON file path (or `-` for stdout).
- `--db DB_FILE`: Target SQLite database file (default: `signalpost.db`).
- `--sources SOURCES_LIST`: Comma-separated data sources to enable (default: `registry,roles,accounts,subentities`).
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
    "schema_version": "1.0.0",
    "run_mode": "live",
    "started_at": "2026-10-07T12:05:02.242847+00:00",
    "completed_at": "2026-10-07T12:06:35.743084+00:00",
    "elapsed_seconds": 93.5,
    "average_seconds_per_company": 0.935,
    "requested_count": 100,
    "processed_count": 100,
    "success_count": 100,
    "failed_count": 0,
    "served_from_local_count": 0,
    "local_fallback_count": 0,
    "live_refreshed_count": 100,
    "companies_with_roles_count": 99,
    "total_role_facts": 438,
    "companies_with_financial_facts_count": 68,
    "total_financial_facts": 2178,
    "companies_with_subentities_count": 67,
    "total_subentities_discovered": 71,
    "total_subentity_facts": 551,
    "company_fetch_requests": 100,
    "roles_fetch_requests": 100,
    "accounts_fetch_requests": 100,
    "subentities_fetch_requests": 100,
    "total_outbound_requests": 400,
    "request_budget_limit": 600,
    "estimated_external_api_cost": "$0",
    "database_identifier": "benchmark_default_final.db",
    "input_source": "benchmark_input_100.json",
    "output_mode": "file"
  },
  "results": [
    {
      "org_number": "923609016",
      "name": "EQUINOR ASA",
      "status": "live_refreshed",
      "source_status": {
        "enhetsregisteret": "success",
        "roles": "success",
        "accounts": "success",
        "subentities": "success",
        "signatur": "disabled",
        "prokura": "disabled",
        "finanstilsynet": "disabled",
        "group": "disabled"
      },
      "facts": [
        {
          "fact_key": "name",
          "value": "EQUINOR ASA",
          "source_name": "Brønnøysund Register Centre - Enhetsregisteret",
          "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
          "retrieved_at": "2026-10-07T12:05:00+00:00",
          "source_validity_date": null,
          "verification_status": "source_asserted"
        }
      ],
      "source_urls": [
        "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
      ],
      "retrieval_dates": [
        "2026-10-07T12:05:00+00:00"
      ],
      "changes": [],
      "warnings": [],
      "errors": [],
      "summary": "Company 'EQUINOR ASA' (923609016): 65 active source-supported facts (7 role facts) (32 financial facts) (12 subentity facts). Refreshed via official registry REST API."
    }
  ]
}
```

---

## 4. Default vs Optional Sources Strategy

- **Default**: `registry,roles,accounts,subentities` (400 requests / 100 companies, 93.5s runtime, 5,074 active facts). Provides high coverage across entity attributes, board roles, financial key figures, and operating locations out of the box.
- **Optional**:
  - `fullmakt`: Adds signature/prokura authority rules (+200 requests / 100 companies).
  - `group`: Adds corporate group structure hierarchy trees (+100 requests / 100 companies).
- **Conditional**:
  - `finanstilsynet`: Financial supervisory authorizations for regulated entities (+100 requests / 100 companies).

---

## 5. API, Model & Financial Cost Details

- **Model Usage**: **$0** (`Signalpost uses no LLM or generative model in the runtime pipeline`).
- **Public APIs**: Brønnøysund Register Centre REST APIs (`Enhetsregisteret`, `Roles`, `Regnskapsregisteret`, `Fullmakttjenesten`, `Underenheter`, `Konsernstruktur`) and Finanstilsynet (`Virksomhetsregisteret v2`).
- **Authentication**: None required (Free public open data under NLOD).
- **Estimated Financial Cost**: **`$0.00`** ($0 API subscription fees and $0 AI model inference fees).

---

## 6. Update & Freshness Proof

- **Live Retrieval Timestamps**: Every fact stores `first_observed_at` and `last_observed_at` ISO 8601 UTC timestamps.
- **Source-Specific Evidence**: Evidence records store exact source URLs and raw JSON evidence payloads (`raw_evidence`).
- **Safe Change Detection**: Same-source changes update values and log `updated` entries in `change_history` without deleting historical lineage.
- **Prior Values Preserved**: `change_history` preserves `previous_value` -> `new_value` audit records.
- **Omissions Do Not Delete**: Missing fields in fresh API responses flag facts as omitted but keep active SQLite entries intact.
- **Source Failures Preserve State**: Network errors or HTTP 500 responses serve cached SQLite profiles without corrupting database state.
- **Complete-Source Removals**: Complete-source responses deactivate absent facts (`is_active=0`) and log `removed` entries only when source semantics confirm entity removal.
- **Historical Accounts**: Financial statements for different fiscal periods (e.g. 2023 vs 2024) coexist as period-specific active facts rather than overwriting prior years.

---

## 7. Explanation Quality & Factual Summaries

Signalpost generates strictly factual summaries based on empirical source evidence:
- **Refreshed via official registry REST API**: Issued when live REST API refresh succeeds.
- **Loaded from local SQLite cache**: Issued when serving stored SQLite profiles in offline mode or fallback.
- **Concise Fact Counts**: Reports exact active fact counts, role facts, financial facts, subentity facts, and group facts without speculation.
- **No Speculation Policy**: Signalpost never infers unstated reasons (e.g. never speculates why revenue changed or why a branch closed).

---

## 8. Reproducing & Validating the 1,000-Profile Bootstrap

The hackathon requires at least 1,000 company profiles. Signalpost includes a deterministic 1,000-profile bootstrap from the local bulk open dataset (`enheter_alle_test.json.gz`) with **0 network requests**:

1. **Manifest Audit**: `data/bootstrap_manifest.json` contains exactly 1,000 unique MOD11 organization numbers.
2. **Export Dataset**: `data/bootstrap_profiles_1000.jsonl` (0.78 MB, 1,000 normalized profile rows).
3. **Export Metadata**: `data/bootstrap_profiles_1000_metadata.json` (SHA-256 hashes, generation timestamp).

To reproduce or validate:
```bash
python -m signal_post --import-manifest data/bootstrap_manifest.json --file enheter_alle_test.json.gz --db signalpost_1000.db
python -m signal_post --validate-db --db signalpost_1000.db --limit 1000
```

---

## 9. Final Canonical Live 100-Company Default Benchmark Results

The final canonical default benchmark evaluated 100 organization numbers on a fresh database (**Enhetsregisteret + Roles API + Regnskapsregisteret + Underenheter**):

- **Command Executed**:
  ```bash
  python -m signal_post --run benchmark_input_100.json --output benchmark_default_final_results.json --db benchmark_default_final.db --sources registry,roles,accounts,subentities --request-budget 600
  ```
- **Input File**: `benchmark_input_100.json` (SHA-256: `76701d63c3dfa74c5348635927818729de72ff8aa25d7a7ab7f07aa6d4b2df68`)
- **Output Result File**: `benchmark_default_final_results.json` (SHA-256: `03003cd6357ce0932c5794fbf562c5a475f2a7353b36cb690f06f1787969b34b`)
- **Database File**: `benchmark_default_final.db`
- **Metadata Provenance File**: `benchmark_default_final_metadata.json`
- **Requested Companies**: `100`
- **Successfully Processed**: `100` (`100.0%` success rate)
- **Identity Matches**: `100 / 100` (`100.0%` match rate)
- **Outbound HTTP Request Breakdown**: `400` total requests (`100` base + `100` roles + `100` accounts + `100` subentities)
- **Wall-Clock Runtime**: **`93.50 seconds`** (Avg `0.9350s` / company)
- **Companies Enriched by Source**:
  - Registry: `100 / 100` (`100.0%`)
  - Roles: `99 / 100` (`99.0%`) -> `438` active role facts
  - Accounts: `68 / 100` (`68.0%`) -> `2,178` active financial facts
  - Subentities: `67 / 100` (`67.0%`) -> `551` active subentity facts (`71` operating units discovered)
- **Total Combined Active Facts Stored**: `5,074` active facts (`1,907` base + `438` roles + `2,178` accounts + `551` subentities)
- **PRAGMA integrity_check**: **`ok`**
- **PRAGMA foreign_key_check**: **`0 violations`**
- **Evidence Linkage Rate**: **`100.0%`** (5,074 / 5,074 facts linked to complete evidence lineage)
- **External API/Model Cost**: **`$0.00`** ($0 API subscription fees & $0 LLM fees)

---

## 10. Live 100-Company Development & Optional Source Benchmarks Summary

| Benchmark Phase | Sources Enabled | Requests Used | Runtime (s) | Active Facts | Key Coverage / Highlights |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Phase 6.2 Baseline** | `registry` | 100 | 26.05s | 1,907 | 100/100 base company profiles |
| **Phase 7 Roles** | `registry,roles` | 200 | 54.77s | 2,345 | 67/100 enriched with board roles (+438 facts) |
| **Phase 9 Finanstilsynet** | `registry,roles,finanstilsynet` | 300 | 94.87s | 2,351 | 1/100 general sample matched (+6 regulatory facts) |
| **Phase 10.2 Accounts** | `registry,roles,accounts` | 300 | 71.94s | 4,523 | 68/100 enriched with annual accounts (+2,178 facts) |
| **Phase 11 Fullmakt** | `registry,roles,accounts,fullmakt` | 500 | 159.93s | 4,596 | 48/100 enriched with authority rules (+73 facts) |
| **Phase 12 Subentities** | `registry,roles,accounts,subentities` | 400 | 163.75s | 5,074 | 67/100 enriched with branches (+551 facts) |
| **Phase 13 Group Structure** *(Dev)* | `registry,roles,accounts,subentities,group` | 500 | 121.70s | 5,273 | 15/100 enriched with corporate groups (+199 facts) |
| **Phase 14 Final Default** | `registry,roles,accounts,subentities` | 400 | 93.50s | 5,074 | **Canonical Final Competition Default Benchmark** |

---

## 11. Hackathon Resource-Limit Proof

Evaluator Constraints vs Measured Default Benchmark:
- **Max Time (45 mins / 2,700s)**: Measured **93.50s** (**3.46%** of limit).
- **Max Requests (2,000 limit)**: Measured **400 requests** (**20.00%** of limit).
- **Max Spend ($10.00 limit)**: Measured **$0.00** (**0.00%** of limit).

---

## 12. Running Unit Tests

Run all offline unit tests (149 passing tests):
```bash
python -m unittest discover -s tests
```

---

## 13. Limitations & Scope

- **Data Source Scope**: Signalpost integrates official open APIs from Brønnøysund (`Enhetsregisteret`, `Roles API`, `Regnskapsregisteret`, `Fullmakttjenesten`, `Underenheter`, `Konsernstruktur`) and Finanstilsynet (`Virksomhetsregisteret v2`).
- **Financial Statement Availability**: Statements are only available for entities required to submit annual accounts to Regnskapsregisteret (e.g. `AS`, `ASA`). Non-reporting entity forms (e.g. sole proprietorships `ENK`) return `success_empty` or `not_found` cleanly.
- **Authority Rule Machine Coverage**: Signature and procuration machine rules in Fullmakttjenesten are available for supported commercial forms (`AS`, `ENK`, `ANS`, etc.). Unmaintained forms (`ORGL`, `ADM`, etc.) return `unsupported_org_form` without failing.
- **Operating Unit Boundaries**: Subentity attributes (addresses, employee counts, activities) are strictly child-scoped and are never merged into parent legal entity attributes.
- **Corporate Group Hierarchy Scope**: Group facts are relationship-scoped (`group_relation_<parent>_<child>_<code...>`), ensuring corporate group relationships never overwrite direct legal entity attributes.
- **Currency & Scaling**: Signalpost preserves exact source-supplied numeric amounts and currencies (`valuta`) without amount scaling or currency conversion. Statements missing currency metadata are withheld from monetary fact publication.
- **Duplicate Resubmissions**: Identical duplicate filings are deduplicated; conflicting resubmissions for the same period/scope are withheld from clean publication to prevent published ambiguity.
- **Synchronous API Execution**: Outbound REST requests execute sequentially per company to maintain strict request budget control.

---

## 14. Repository & Submission Information

- **Repository URL**: `https://github.com/pvsatvika/signalpost`
- **Branch**: `main`
- **Submission Commit**: `9f62111dfd6b4f84b25ce1d50cdf3960f8756949`

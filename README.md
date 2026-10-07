# Signalpost

**Signalpost** is an open-source agent system designed to find, verify, and maintain public information about Norwegian companies using official public data sources.

---

## 1. What Signalpost Does

Signalpost integrates official Norwegian public registries into a normalized company graph backed by SQLite, providing:
- **100% Deterministic Extraction**: Zero commercial LLM API dependencies, zero paid subscription fees, and $0 runtime cost.
- **Fact-Level Evidence Lineage**: Every published fact is linked directly to an exact source URL, retrieval timestamp, and raw source JSON payload (`raw_evidence`).
- **Safe Profile Refresh Workflows**: Chronological change detection with factual explanation reports.
- **Privacy Minimization**: Automatic stripping of birth dates (`fodselsdato`), national identity numbers (`fnr`), and D-numbers.
- **Budget Protection**: Local request budget tracker enforcing API limits ($0 cost, hackathon compliant).

---

## 2. Why Signalpost is Safe

1. **Identity & Format Verification**: Validates 9-digit Norwegian organization numbers via MOD11 algorithm before network requests or database transactions occur.
2. **No Hallucinations**: All assertions originate directly from official government REST APIs or official bulk open datasets.
3. **No Accidental Deletions**: Omitted fields in new registry responses are flagged as omitted but remain stored in SQLite to prevent data loss.
4. **Historical Financial Protection**: Financial statements preserve exact source numeric amounts and currency codes (`valuta`). Statements missing currency metadata or containing conflicting duplicate filings are withheld from monetary fact publication.
5. **Operating Unit & Group Boundaries**: Subentities (`underenheter`) and corporate group relations (`konsernstruktur`) are stored as relationship-scoped facts and are **never merged into parent legal entity attributes**.

---

## 3. Quick Start

### Requirements
- **Python 3.9+**

### Installation

#### Windows (PowerShell / Command Prompt)
```powershell
pip install -r requirements.txt
```

#### Linux / macOS
```bash
pip install -r requirements.txt
```

---

## 4. One-Command Evaluator Execution

Process organization numbers safely through validation, multi-source enrichment, evidence formatting, and budget tracking:

```bash
python -m signal_post --run input.json --output results.json --db signalpost.db --request-budget 600
```

### Stdin / Stdout Execution
```bash
cat input.json | python -m signal_post --run - --output - --db signalpost.db --request-budget 600
```

---

## 5. Example Input

Accepts a JSON array of 9-digit Norwegian organization numbers (`input.json`):

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

---

## 6. Example Output

The runner outputs evaluator-ready structured JSON with execution metrics and company facts (`results.json`):

```json
{
  "run_metrics": {
    "schema_version": "1.0.0",
    "run_mode": "live",
    "elapsed_seconds": 0.935,
    "average_seconds_per_company": 0.935,
    "requested_count": 1,
    "processed_count": 1,
    "success_count": 1,
    "failed_count": 0,
    "served_from_local_count": 0,
    "local_fallback_count": 0,
    "live_refreshed_count": 1,
    "total_outbound_requests": 4,
    "estimated_external_api_cost": "$0"
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

## 7. Integrated Public Data Sources

### Default Sources (`--sources registry,roles,accounts,subentities`)
1. **Brønnøysund Enhetsregisteret**: Official company registry (`GET https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}`).
2. **Brønnøysund Roles API**: Official board and leadership roles (`GET https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}/roller`). Strips birth dates and national identity numbers.
3. **Brønnøysund Regnskapsregisteret**: Official annual accounts key figures (`GET https://data.brreg.no/regnskapsregisteret/regnskap/{org_number}`). Strictly 11 whitelisted source key figures.
4. **Brønnøysund Underenheter API**: Official operating units and branches (`GET https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org_number}`). Strictly child-scoped.

### Optional / Conditional Sources
5. **Brønnøysund Fullmakttjenesten**: Official signature (`/signatur`) and procuration (`/prokura`) authority rules. Optional (`--sources ...,fullmakt`).
6. **Brønnøysund Corporate Group Structure**: Official corporate group hierarchy trees (`GET https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org_number}`). Optional (`--sources ...,group`).
7. **Finanstilsynet Virksomhetsregisteret v2**: Financial supervisory authorizations (`GET https://api.finanstilsynet.no/registry/v2/legal-entities/filter?query={org_number}`). Optional/conditional (`--sources ...,finanstilsynet`).

---

## 8. Evidence Model

Signalpost uses SQLite (`signalpost.db` by default) with relational tables:
1. **`companies`**: Primary company records keyed by 9-digit `org_number`.
2. **`sources`**: Registered public data sources.
3. **`facts`**: Granular key-value assertions indexed via a partial unique index `(org_number, fact_key) WHERE is_active = 1`.
4. **`evidence`**: Lineage links containing exact source URLs, retrieval timestamps, validity dates, and raw open-data payloads (`raw_evidence`).
5. **`change_history`**: Audit log recording `previous_value` -> `new_value`, change timestamps, and change explanations (`created`, `updated`, `reasserted`, `conflict`, `removed`).
6. **`collection_queue`**: Discovery and collection queue.
7. **`request_log`**: Daily outbound HTTP request tracker enforcing request limits.

---

## 9. Update & Change Detection Model

- **Live Timestamps**: Every fact stores `first_observed_at` and `last_observed_at` ISO 8601 UTC timestamps.
- **Source Lineage**: Evidence rows link facts directly to official government endpoints.
- **Chronological Same-Source Updates**: Same-source value changes deactivate old facts, activate new facts, and record `updated` audit entries.
- **Multi-Source Conflicts**: Conflicting values from different sources are flagged as `conflicting` without silently overwriting existing data.
- **Omission Safety**: Missing fields in new API responses flag facts as omitted but keep active SQLite entries intact.
- **Historical Accounts Protection**: Financial statements for different fiscal periods (e.g. 2023 vs 2024) coexist as period-specific active facts rather than overwriting prior years.

---

## 10. Default vs Optional Sources Rationale

| Source | Coverage ROI | Request Cost | Decision |
| :--- | :--- | :--- | :--- |
| **Registry** | 100/100 enriched, +1,907 facts | 1 request/company | **Default** |
| **Roles** | 67/100 enriched, +438 facts | 1 request/company | **Default** |
| **Accounts** | 68/100 enriched, +2,178 financial facts | 1 request/company | **Default** |
| **Subentities** | 67/100 enriched, +551 operating unit facts | 1 request/company | **Default** |
| **Fullmakt** | 48/100 enriched, +73 authority facts | 2 requests/company | **Optional** (`--sources ...,fullmakt`) |
| **Group** | 15/100 enriched, +199 hierarchy facts | 1 request/company | **Optional** (`--sources ...,group`) |
| **Finanstilsynet** | 1/100 general sample enriched, +6 facts | 1 request/company | **Conditional** (`--sources ...,finanstilsynet`) |

---

## 11. 1,000-Profile Bootstrap

Signalpost includes a deterministic 1,000-profile bootstrap mechanism operating locally with **0 network requests**:
1. **Manifest**: `data/bootstrap_manifest.json` (1,000 unique valid MOD11 org numbers).
2. **Export Dataset**: `data/bootstrap_profiles_1000.jsonl` (0.78 MB, 1,000 normalized JSONL profile rows).
3. **Export Metadata**: `data/bootstrap_profiles_1000_metadata.json` (SHA-256 hashes, record count, generation timestamp).

To reproduce or validate:
```bash
python -m signal_post --generate-manifest --file enheter_alle_test.json.gz --limit 1000
python -m signal_post --import-manifest data/bootstrap_manifest.json --file enheter_alle_test.json.gz --db signalpost_1000.db
python -m signal_post --validate-db --db signalpost_1000.db --limit 1000
```

---

## 12. Benchmarks

### Canonical Final Default Benchmark (`benchmark_default_final.db`, `benchmark_default_final_results.json`)
- **Command**: `python -m signal_post --run benchmark_input_100.json --output benchmark_default_final_results.json --db benchmark_default_final.db --sources registry,roles,accounts,subentities --request-budget 600`
- **Requested / Processed**: 100 / 100 (`100%` success rate)
- **Outbound HTTP Requests**: 400 total requests (100 base + 100 roles + 100 accounts + 100 subentities)
- **Wall-Clock Runtime**: **`93.50 seconds`** (0.935s / company)
- **Total Active Facts Stored**: **`5,074` active facts** (100% evidence linkage rate)
- **SQLite Integrity**: `PRAGMA integrity_check = ok` (0 foreign key violations)
- **External API/Model Cost**: **`$0.00`**

---

## 13. Running Tests

The test suite runs 100% offline (149 passing unit tests) using mocked HTTP responses and temporary SQLite databases.

Run all tests:
```bash
python -m unittest discover -s tests
```

---

## 14. Cost Analysis

| Component | Cost |
| :--- | :--- |
| **Brønnøysund Public REST APIs** | `$0.00` (Free public Open Data under NLOD) |
| **Finanstilsynet Registry API v2** | `$0.00` (Free public Open API) |
| **Local SQLite Database** | `$0.00` (Local embedded database) |
| **LLM / Model Fees** | `$0.00` (Zero commercial LLM API fees or subscriptions) |
| **Total Runtime Spend** | **`$0.00`** |

---

## 15. Resource Limits Compliance

Evaluator Constraints vs Measured Default Benchmark:
- **Max Time (45 mins / 2,700s)**: Measured **93.50s** (**3.46%** of limit).
- **Max Requests (2,000 limit)**: Measured **400 requests** (**20.00%** of limit).
- **Max Spend ($10.00 limit)**: Measured **$0.00** (**0.00%** of limit).

---

## 16. Licence & Attribution

- **Source Attribution**: Contains data from official public registers maintained by the Brønnøysund Register Centre (*Brønnøysundregistrene*) and Financial Supervisory Authority of Norway (*Finanstilsynet*), made available under the Norwegian Licence for Open Government Data (NLOD).
- **Licence**: Open-Source (MIT Licence).

---

## 17. Repository & Submission Information

- **Repository URL**: `https://github.com/pvsatvika/signalpost`
- **Branch**: `main`
- **Submission Commit**: `9f62111dfd6b4f84b25ce1d50cdf3960f8756949`

# Signalpost

**Signalpost** is an open-source agent system designed to find, verify, and maintain public information about Norwegian companies using official public data sources.

---

## Architecture & Overview

Signalpost integrates official public data sources (starting with the **Brønnøysund Register Centre / Enhetsregisteret**) into a normalized company graph with **fact-level evidence storage** and **safe profile refresh workflows** backed by SQLite.

### Key Components

- **`signal_post.client.BrregClient`**: Handles 9-digit organization number validation, HTTP communication, timeout & retry handling, and identity verification.
- **`signal_post.models.NormalizedCompanyProfile`**: Pydantic schema mapping registry fields into normalized models while retaining original raw JSON evidence.
- **`signal_post.storage`**: SQLite storage module managing relational tables for companies, sources, individual facts, evidence lineage, change history, collection queue, and request budget log.
- **`signal_post.refresh`**: Safe company refresh workflow, change detection, and structured change explanation report generator.
- **`signal_post.discovery`**: Modular company discovery searching Brønnøysund Enhetsregisteret API, validating 9-digit numbers, deduplicating, and queuing candidates into SQLite.
- **`signal_post.collection`**: Controlled, resumable batch collection workflow executing profile fetching, validation, and SQLite evidence storage.
- **`signal_post.bulk`**: Memory-efficient streaming bulk open-data importer for Brønnøysund Enhetsregisteret bulk files (`https://data.brreg.no/enhetsregisteret/api/enheter/lastned`), supporting zero-cost local dataset reuse, bounded imports, and conservative live profile protection.
- **`signal_post.budget`**: Daily outbound HTTP request budget tracker enforcing API request limits ($0 cost, hackathon compliant).
- **`signal_post.cli`**: CLI supporting fetching, storing, refreshing, discovering, collecting, bulk downloading, bulk importing, resuming queue processing, checking collection status, inspecting active facts & evidence, inspecting change history, and exporting JSON.

---

## Database Schema & Evidence Architecture

Signalpost uses a local SQLite database (`signalpost.db` by default) with 7 core tables:

1. **`companies`**: Primary records keyed by 9-digit `org_number`, tracking `first_seen_at` and `last_checked_at`.
2. **`sources`**: Registered public data sources:
   - `brreg_enhetsregisteret`: Official Brønnøysund Enhetsregisteret REST API.
   - `brreg_bulk_enhetsregisteret`: Official Brønnøysund Enhetsregisteret Bulk Open Data dataset (`https://data.brreg.no/enhetsregisteret/api/enheter/lastned`).
3. **`facts`**: Granular key-value assertions (`fact_id`, `org_number`, `fact_key`, `fact_value`, `verification_status`, `first_observed_at`, `last_observed_at`, `is_active`). Active facts are indexed via a partial unique index `(org_number, fact_key) WHERE is_active = 1`.
4. **`evidence`**: Lineage links for facts containing the exact source URL, retrieval timestamp, validity date, and raw JSON payload (`raw_evidence`).
5. **`change_history`**: Audit log recording `previous_value` -> `new_value`, change timestamp, and change type (`created`, `updated`, `reasserted`, `conflict`).
6. **`collection_queue`**: Discovery queue managing organization numbers (`org_number`, `name`, `organization_form_code`, `registration_date`, `discovered_at`, `discovery_source`, `status`, `attempt_count`, `last_attempt_at`, `error_message`).
7. **`request_log`**: Daily outbound HTTP request tracker recording request types, target URLs, and timestamps.

### Verification Status Lifecycle

- **`source_asserted`**: Fact is asserted by a single data source.
- **`corroborated`**: Fact is confirmed by multiple independent data sources asserting the exact same value.
- **`conflicting`**: Different data sources assert conflicting values for the same fact key. The conflict is recorded in evidence and logged in `change_history` without silently overwriting existing data.

---

## Setup & Installation

### Requirements

- Python 3.9+

### Environment Setup

1. Navigate to the project root directory:
   ```bash
   cd signal-post
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

---

## Running the Application

### Evaluator Batch Runner (One-Command Execution)

Process one or more organization numbers, enforce outbound request budget, and generate structured report JSON:

```bash
python -m signal_post --run input.json --output results.json --db signalpost.db --request-budget 150
```

### Fetch & Display Company Profile (Human Summary)

```bash
python -m signal_post 974760673
```

### Fetch, Normalize & Store to SQLite

Fetch live company data from Brønnøysund, normalize the response, store individual facts, and record evidence into SQLite:

```bash
python -m signal_post 974760673 --db signalpost.db --store
```

### Safely Refresh Profile & Generate Change Report

Fetch latest data, detect changes against stored facts, commit valid updates, and print structured change explanations:

```bash
python -m signal_post 974760673 --db signalpost.db --refresh
```

*Example Refresh Output (When Changes Detected):*
```text
======================================================================
 COMPANY PROFILE REFRESH REPORT: EQUINOR ASA
======================================================================
 Organization Number : 923609016
 Status              : SUCCESS
 Retrieval Time      : 2026-10-04T12:00:00Z
 Source              : Brønnøysundregistrene - Enhetsregisteret (brreg_enhetsregisteret)
 Exact Source URL    : https://data.brreg.no/enhetsregisteret/api/enheter/923609016
 Summary Counts      : updated=1, unchanged=21
----------------------------------------------------------------------
 Detected Fact Changes & Explanations:

  [UPDATED]    Fact: num_employees
    Explanation: Fact 'num_employees' changed from '21272' to '21500' according to the latest record from 'Brønnøysundregistrene - Enhetsregisteret'.
    Previous   : 21272
    New Value  : 21500
======================================================================
```

*Example Refresh Output (When Unchanged):*
```text
======================================================================
 COMPANY PROFILE REFRESH REPORT: REGISTERENHETEN I BRØNNØYSUND
======================================================================
 Organization Number : 974760673
 Status              : SUCCESS
 Retrieval Time      : 2026-10-04T12:00:00Z
 Source              : Brønnøysundregistrene - Enhetsregisteret (brreg_enhetsregisteret)
 Exact Source URL    : https://data.brreg.no/enhetsregisteret/api/enheter/974760673
 Summary Counts      : unchanged=22
----------------------------------------------------------------------
 Result: No profile changes detected. All active facts remain unchanged.
======================================================================
```

### Export Refresh Report as JSON

```bash
python -m signal_post 974760673 --db signalpost.db --refresh --json
```

### Inspect Stored Facts & Evidence Lineage

```bash
python -m signal_post 974760673 --db signalpost.db --inspect-facts
```

### Inspect Change History Audit Log

```bash
python -m signal_post 974760673 --db signalpost.db --inspect-history
```

### Discover Companies from Brønnøysund API

Search the official Brønnøysund search endpoint, validate organization numbers, deduplicate, and queue candidates:

```bash
python -m signal_post --discover --limit 50 --filter-org-form AS --db signalpost.db
```

### Process Queued Profiles (Batch Collection)

Collect profiles for queued organization numbers using the safe refresh workflow and request budget tracking:

```bash
python -m signal_post --collect --limit 10 --request-budget 50 --db signalpost.db
```

### Resume Failed or Interrupted Collection

Resume processing pending or previously failed queue items:

```bash
python -m signal_post --resume --limit 20 --db signalpost.db
```

### Inspect Collection & Queue Status Summary

Print real-time queue breakdown, stored profile count, and today's outbound request budget:

```bash
python -m signal_post --status --db signalpost.db
```

### Download Official Bulk Open Data Dataset

Download the official Brønnøysund bulk dataset file (`https://data.brreg.no/enhetsregisteret/api/enheter/lastned`) with HTTP request budget tracking:

```bash
python -m signal_post --bulk-download --file enheter_alle.json.gz --db signalpost.db
```

### Import Profiles from Local Bulk File

Stream and import profiles from a previously downloaded local bulk file with zero additional HTTP requests:

```bash
python -m signal_post --bulk-import enheter_alle.json.gz --limit 100 --filter-org-form AS --db signalpost.db
```

---

## Open Data License

The bulk open data datasets provided by the Brønnøysund Register Centre (Enhetsregisteret) are distributed under the **Norwegian Licence for Open Government Data (NLOD)** (*Norsk lisens for åpne offentlige data*).

Signalpost complies fully with NLOD requirements:
- Source attribution (`source_id='brreg_bulk_enhetsregisteret'`).
- Exact retrieval timestamps and source URL metadata stored for every evidence record.
- Unaltered retention of raw open-data payloads in SQLite (`raw_evidence`).

---

## Bounded Bulk Importer & 1,000-Profile Bootstrap (Phase 5.1)

Signalpost includes a reproducible bulk selection and import engine for scaling company coverage without consuming API budget:

1. **Deterministic Selection Strategy**:
   - **Pass 1 Analysis**: Scans local bulk dataset (`enheter_alle_test.json.gz`) to count active organization forms (`ENK`, `AS`, `FLI`, `ESEK`, `UTLA`, `NUF`, `DA`, etc.).
   - **Quota Allocation**: Allocates representation proportionally across all organization forms with a minimum base allocation per form.
   - **Pass 2 Selection**: Streams dataset to select candidate organization numbers matching form quotas deterministically.

2. **Selection Manifest (`data/bootstrap_manifest.json`)**:
   - Machine-readable manifest storing:
     - `generated_at`: ISO timestamp of manifest creation.
     - `source_file`: Path to bulk file.
     - `requested_count` / `actual_count`: Target profile counts (1,000).
     - `org_form_distribution`: Breakdown across organization form codes.
     - `selected_org_numbers`: Ordered array of selected 9-digit organization numbers.

3. **Commands**:
   - **Generate Selection Manifest**:
     ```bash
     python -m signal_post --generate-manifest --file enheter_alle_test.json.gz --limit 1000
     ```
   - **Import Profiles from Manifest (0 HTTP Requests)**:
     ```bash
     python -m signal_post --import-manifest data/bootstrap_manifest.json --file enheter_alle_test.json.gz --db signalpost_1000.db
     ```
   - **Validate Database Integrity**:
     ```bash
     python -m signal_post --validate-db --db signalpost_1000.db --limit 1000
     ```

4. **Database Integrity Report (`signalpost_1000.db`)**:
   - `PRAGMA integrity_check`: `ok` (PASSED)
   - Foreign Key Violations: `0` (PASSED)
   - Total Stored Companies: `1,000` (100% match)
   - Total Active Facts: `17,725`
   - Total Evidence Rows: `17,725` (100.0% evidence linkage rate)
   - Facts per Company: Min `14`, Avg `17.73`, Max `26`

---

## Safe Refresh & Change Detection Mechanics

1. **Identity & Format Verification**: Validates organization number format and verifies returned `organisasjonsnummer` matches the requested number before any DB transactions occur.
2. **Protection Against False Changes**:
   - **Missing Fields**: Omitted fields in new responses are flagged as `omitted` in change reports but are **never deleted or deactivated in SQLite**. Existing active facts remain stored.
   - **Unchanged Values**: Unchanged values update observation timestamps and append evidence, but do **not** generate duplicate change-history entries.
   - **Chronological Same-Source Updates**: Value changes from the same source deactivate old facts, activate new facts, and record `updated` entries with old/new values and factual explanations.
   - **Multi-Source Conflicts**: Conflicting values from a different source are flagged as `conflicting` without overwriting the existing stored fact.
3. **Failed Refreshes**: HTTP timeouts, connection errors, or identity mismatches fail the refresh gracefully without modifying existing database state.

---

## Python API Usage

```python
from signal_post import BrregClient, get_connection, init_db, refresh_company

conn = get_connection("signalpost.db")
init_db(conn)

# Safely refresh profile and receive structured change report
result = refresh_company(conn, "923609016")

print(f"Refresh Status: {result.status}")
print(f"Summary: {result.summary_counts}")
for change in result.changes:
    if change.change_type != "unchanged":
        print(f"Change ({change.change_type}): {change.explanation}")

conn.close()
```

---

## Running Tests

The test suite runs 100% offline (78 passing unit tests) using mocked HTTP responses and temporary SQLite databases.

Run all tests:
```bash
python -m unittest discover -s tests
```

---

## Known Limitations

- **Single Primary Public Source**: Currently integrated strictly with Brønnøysund `Enhetsregisteret`. Additional sources (e.g., `Underenheter`, financial statements) will be integrated in subsequent phases.
- **Synchronous Execution**: Operations process individual organization numbers synchronously.


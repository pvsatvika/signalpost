# Model and API Details - Signalpost

This document details the architectural specifications, API requirements, model dependencies, and operational costs for **Signalpost**.

---

## 1. Core Pipeline Architecture & Model Requirements

- **Model Requirement**: **No paid LLM or commercial AI API is required** for Signalpost's core extraction, normalization, storage, change detection, discovery, collection, or verification pipeline.
- **Runtime Statement**: `Signalpost uses no LLM or generative model in the runtime pipeline.`
- **Deterministic Processing Engine**: Pure Python code performs deterministic registry schema parsing, 9-digit Norwegian organization number format validation via MOD11 algorithm, Pydantic type coercion, relational database transaction management, and lineage tracking.
- **Development Tools**: Antigravity agent tools used during development are not part of runtime.

---

## 2. External Data Sources & Public APIs

### Primary Registry REST API (Default Source: `registry`)
- **Provider**: Brønnøysund Register Centre (*Brønnøysundregistrene / Enhetsregisteret*).
- **Endpoint Pattern**: `https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Transport**: HTTPS GET returning JSON payloads.
- **Budget Control**: Logged under `company_fetch` request type.

### Bulk Open Data Dataset
- **Provider**: Brønnøysund Register Centre Enhetsregisteret Open Data.
- **Download Endpoint**: `https://data.brreg.no/enhetsregisteret/api/enheter/lastned`
- **Licence**: Norwegian Licence for Open Government Data (NLOD).
- **Local Reuse**: Signalpost streams compressed bulk open data files (`enheter_alle_test.json.gz`) locally with O(1) RAM usage, requiring **0 HTTP requests** for local bootstrap selection and mass importing.

### Roles Open Data REST API (Default Source: `roles`)
- **Provider**: Brønnøysund Register Centre (*Brønnøysundregistrene / Enhetsregisteret*).
- **Endpoint Pattern**: `https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}/roller`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Privacy Enforcement**: Strips birth dates (`fodselsdato`) and national identity numbers (`fnr`) before facts or evidence are normalized or stored.
- **Budget Control**: Logged under `roles_fetch` request type.

### Regnskapsregisteret REST API (Default Source: `accounts`)
- **Provider**: Brønnøysund Register Centre (*Regnskapsregisteret*).
- **Endpoint Pattern**: `https://data.brreg.no/regnskapsregisteret/regnskap/{org_number}`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Accuracy & Precision**: Strictly 11 whitelisted source key figures; 0 derived ratios, 0 profit margins, 0 OCR/parsing errors. Exact source numeric amounts and currencies (`valuta`) preserved. Statements missing currency metadata or containing conflicting duplicate filings are withheld from monetary fact publication.
- **Budget Control**: Logged under `accounts_fetch` request type.

### Underenheter Operating Units REST API (Default Source: `subentities`)
- **Provider**: Brønnøysund Register Centre (*Enhetsregisteret / Underenheter*).
- **Endpoint Pattern**: `https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org_number}`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Parent/Child Scope Separation**: Subentity facts (addresses, employee counts, activities) strictly preserve `relationship = operating_unit_of` and are never merged into parent legal entity attributes.
- **Employee Suppression Semantics**: Respects `harRegistrertAntallAnsatte=True` without inventing an exact zero when `antallAnsatte` is suppressed by Brønnøysund.
- **Budget Control**: Logged under `subentities_fetch` request type.

### Fullmakttjenesten Signature Rights & Procuration REST API (Optional Source: `fullmakt`)
- **Provider**: Brønnøysund Register Centre (*Fullmakttjenesten*).
- **Endpoint Patterns**:
  - `https://data.brreg.no/fullmakt/enheter/{org_number}/signatur`
  - `https://data.brreg.no/fullmakt/enheter/{org_number}/prokura`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Privacy Enforcement**: Strict privacy barrier recursively strips birth dates (`fodselsdato`), national identity numbers (`fnr`), and D-numbers (`d-number`).
- **Budget Control**: Logged under `signatur_fetch` and `prokura_fetch` request types.

### Corporate Group Structure REST API (Optional Source: `group`)
- **Provider**: Brønnøysund Register Centre (*Enhetsregisteret / Konsernstruktur*).
- **Endpoint Pattern**: `https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org_number}`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Relationship Scoping**: Group facts are stored as relationship-scoped entries (`group_relation_<parent>_<child>_<code...>`), ensuring corporate group relationships never overwrite direct legal entity attributes.
- **Cycle Protection & Caps**: Implements set-based cycle detection and bounded safety limits (`max_nodes=500`, `max_depth=20`).
- **Budget Control**: Logged under `group_structure_fetch` request type.

### Finanstilsynet Virksomhetsregisteret API v2 (Conditional Source: `finanstilsynet`)
- **Provider**: Financial Supervisory Authority of Norway (*Finanstilsynet*).
- **Endpoint Pattern**: `https://api.finanstilsynet.no/registry/v2/legal-entities/filter?query={org_number}`
- **Authentication**: **None** (Public Open Data REST API).
- **Scope**: Exact 9-digit company matching (`legalEntityType != 'Person'`). Excludes residential addresses, birth dates, and personal identification numbers.
- **Budget Control**: Logged under `finanstilsynet_fetch` request type.

---

## 3. Financial Cost Analysis

| Component | Cost | Default Enabled |
| :--- | :--- | :--- |
| **Brønnøysund Registry REST API** | `$0.00` | Yes |
| **Brønnøysund Bulk Open Data** | `$0.00` | Yes (Local) |
| **Brønnøysund Roles Open API** | `$0.00` | Yes |
| **Brønnøysund Regnskapsregisteret REST API** | `$0.00` | Yes |
| **Brønnøysund Underenheter Open API** | `$0.00` | Yes |
| **Brønnøysund Fullmakttjenesten Open API** | `$0.00` | Optional |
| **Brønnøysund Corporate Group Structure Open API** | `$0.00` | Optional |
| **Finanstilsynet Registry API v2** | `$0.00` | Conditional |
| **Database Storage (SQLite)** | `$0.00` | Yes |
| **Model / Inference Fees** | `$0.00` | N/A |
| **Total External Financial Cost** | **`$0.00`** | **`$0.00`** |

---

## 4. Dependencies & Open-Source Libraries

Signalpost is built using standard Python and permissive open-source libraries:
- **`Python 3.9+`**: Minimum supported Python runtime version.
- **`pydantic (>=2.0.0)`**: Strict data validation, schema enforcement, and type coercion.
- **`requests (>=2.28.0)`**: HTTP transport with custom session timeout and budget tracking.
- **`sqlite3`** (Standard Library): Relational database with full transaction safety, partial unique indexes, and foreign key enforcement.
- **`gzip`, `json`, `re`, `datetime`, `argparse`, `unittest`** (Standard Library): Streaming compression, JSON serialization, regex validation, date formatting, CLI parsing, and test execution.

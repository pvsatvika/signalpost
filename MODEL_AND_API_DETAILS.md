# Model and API Details - Signalpost

This document details the architectural specifications, API requirements, model dependencies, and operational costs for **Signalpost**.

---

## 1. Core Pipeline Architecture & Model Requirements

- **Model Requirement**: **No paid LLM or commercial AI API is required** for Signalpost's core extraction, normalization, storage, change detection, discovery, collection, or verification pipeline.
- **Deterministic Processing Engine**: Pure Python code performs deterministic registry schema parsing, 9-digit Norwegian organization number format validation, Pydantic type coercion, relational database transaction management, and lineage tracking.
- **Future AI Integration**: The system is designed to allow optional local or open-weights LLMs to be attached for downstream natural-language analysis without altering the underlying SQLite evidence graph.

---

## 2. External Data Sources & Public APIs

### Primary Registry REST API
- **Provider**: Brønnøysund Register Centre (*Brønnøysundregistrene / Enhetsregisteret*).
- **Endpoint Pattern**: `https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}`
- **Authentication**: **None** (Public Open Data REST API).
- **Transport**: HTTPS GET returning JSON payloads.
- **Rate Limit / Budget Control**: Built-in daily request budget tracker enforcing conservative limits (hackathon daily limit: 2,000 requests; Signalpost default: 100–150 requests per evaluation run).

### Bulk Open Data Dataset
- **Provider**: Brønnøysund Register Centre Enhetsregisteret Open Data.
- **Download Endpoint**: `https://data.brreg.no/enhetsregisteret/api/enheter/lastned`
- **Licence**: **Norwegian Licence for Open Government Data (NLOD)** (*Norsk lisens for åpne offentlige data*).
- **Local Reuse**: Signalpost streams compressed bulk open data files (`enheter_alle.json.gz`) locally with memory-bounded streaming (O(1) RAM usage), requiring **0 HTTP requests** for local bootstrap selection and mass importing.

### Roles Open Data REST API
- **Provider**: Brønnøysund Register Centre (*Brønnøysundregistrene / Enhetsregisteret*).
- **Endpoint Pattern**: `https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}/roller`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Transport**: HTTPS GET returning JSON payloads.
- **Privacy Enforcement**: Strips birth dates (`fodselsdato`) and national identity numbers (`fnr`) before facts or evidence are normalized or stored.
- **Holder-Stable Keys**: Hashes holder identity to avoid false changes when board members leave or change ordering.

### Finanstilsynet Virksomhetsregisteret API v2
- **Provider**: Financial Supervisory Authority of Norway (*Finanstilsynet*).
- **Endpoint Pattern**: `https://api.finanstilsynet.no/registry/v2/legal-entities/filter?query={org_number}`
- **Authentication**: **None** (Public Open Data REST API).
- **Transport**: HTTPS GET returning OpenAPI 3.0 JSON payloads.
- **Scope & Privacy**: Exact 9-digit company matching (`legalEntityType != 'Person'`). Excludes residential addresses, birth dates, and personal identification numbers.
- **Licence Facts**: Extracts Finanstilsynet ID, LEI code, and regulatory licences/authorisations.

### Regnskapsregisteret REST API
- **Provider**: Brønnøysund Register Centre (*Regnskapsregisteret*).
- **Endpoint Pattern**: `https://data.brreg.no/regnskapsregisteret/regnskap/{org_number}`
- **Authentication**: **None** (Public Open Data REST API under NLOD).
- **Duplicate & Revision Handling**: Identical duplicate filings for a period/scope are deduplicated deterministically; conflicting duplicate filings for the same period and scope are withheld from clean publication to prevent published ambiguity.

### Fullmakttjenesten Signature Rights & Procuration REST API
- **Provider**: Brønnøysund Register Centre (*Fullmakttjenesten*).
- **Endpoint Patterns**:
  - `https://data.brreg.no/fullmakt/enheter/{org_number}/signatur`
  - `https://data.brreg.no/fullmakt/enheter/{org_number}/prokura`
- **Authentication**: **None** (Public Open Data REST API under NLOD). Restrictive Maskinporten endpoints (`/autorisert-api/` or `data.vcert.brreg.no`) are deliberately NOT used.
- **Transport**: HTTPS GET returning structured JSON authority objects.
- **Privacy Enforcement**: Strict privacy barrier recursively strips birth dates (`fodselsdato`, `fødselsdato`), national identity numbers (`fnr`, `fodselsnummer`), and D-numbers (`d-number`, `dnummer`) before any fact or evidence is stored in SQLite or raw evidence.
- **Organization Form Pre-checking**: Uses Enhetsregisteret organization form classification to skip unnecessary HTTP requests for forms that do not maintain machine authority routines (e.g. `ORGL`, `ADM`, `STAT`, `FYLK`, `KOMM`, `PERS`).
- **Holder-Stable Keys**: Normalized combination and text rule facts keyed deterministically to avoid false changes.

---

## 3. Financial Cost Analysis

| Component | Cost |
| :--- | :--- |
| **Brønnøysund Registry REST API** | `$0.00` (Free public API under NLOD) |
| **Brønnøysund Bulk Open Data** | `$0.00` (Free public dataset under NLOD) |
| **Brønnøysund Roles Open API** | `$0.00` (Free public API under NLOD) |
| **Brønnøysund Regnskapsregisteret REST API** | `$0.00` (Free public API under NLOD) |
| **Finanstilsynet Registry API v2** | `$0.00` (Free public Open API) |
| **Database Storage (SQLite)** | `$0.00` (Local embedded database) |
| **Model / Inference Fees** | `$0.00` (Zero commercial LLM API fees or subscriptions) |
| **Total External Financial Cost** | **`$0.00`** |

---

## 4. Dependencies & Open-Source Libraries

Signalpost is built using standard Python and permissive open-source libraries:

- **`Python 3.9+`**: Core runtime.
- **`pydantic (>=2.0.0)`**: Strict data validation, schema enforcement, and type coercion.
- **`requests (>=2.28.0)`**: HTTP transport with custom session timeout and budget tracking.
- **`sqlite3`** (Standard Library): Relational database with full transaction safety, partial unique indexes, and foreign key enforcement.
- **`gzip`, `json`, `re`, `datetime`, `argparse`, `unittest`** (Standard Library): Streaming compression, JSON serialization, regex validation, date formatting, CLI parsing, and test execution.

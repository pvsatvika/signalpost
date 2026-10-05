"""
Signalpost - Agent for Norwegian company identification and verification.
"""

from signal_post.client import BrregClient
from signal_post.exceptions import (
    SignalpostError,
    InvalidOrgNumberError,
    CompanyNotFoundError,
    RegistryClientError,
    DataMismatchError,
)
from signal_post.models import (
    NormalizedCompanyProfile,
    Address,
    IndustryCode,
    OrganizationForm,
    CapitalInfo,
    HistoricalName,
)
from signal_post.storage import (
    get_connection,
    init_db,
    save_company_profile,
    get_company,
    get_facts,
    get_change_history,
    get_company_profile_with_evidence,
)
from signal_post.refresh import (
    refresh_company,
    FactChangeItem,
    RefreshResult,
)
from signal_post.budget import (
    RequestBudgetTracker,
    RequestBudgetExceededError,
)
from signal_post.discovery import (
    discover_organizations,
)
from signal_post.collection import (
    collect_queued_profiles,
    get_collection_status,
)
from signal_post.bulk import (
    stream_bulk_file,
    download_bulk_dataset,
    import_bulk_profiles,
    select_bootstrap_candidates,
    generate_bootstrap_manifest,
    import_bulk_from_manifest,
    validate_database_integrity,
)

from signal_post.runner import (
    CompanyRunner,
    run_evaluation,
    load_input_org_numbers,
)

__version__ = "0.1.0"

__all__ = [
    "BrregClient",
    "SignalpostError",
    "InvalidOrgNumberError",
    "CompanyNotFoundError",
    "RegistryClientError",
    "DataMismatchError",
    "NormalizedCompanyProfile",
    "Address",
    "IndustryCode",
    "OrganizationForm",
    "CapitalInfo",
    "HistoricalName",
    "get_connection",
    "init_db",
    "save_company_profile",
    "get_company",
    "get_facts",
    "get_change_history",
    "get_company_profile_with_evidence",
    "refresh_company",
    "FactChangeItem",
    "RefreshResult",
    "RequestBudgetTracker",
    "RequestBudgetExceededError",
    "discover_organizations",
    "collect_queued_profiles",
    "get_collection_status",
    "stream_bulk_file",
    "download_bulk_dataset",
    "import_bulk_profiles",
    "select_bootstrap_candidates",
    "generate_bootstrap_manifest",
    "import_bulk_from_manifest",
    "validate_database_integrity",
    "CompanyRunner",
    "run_evaluation",
    "load_input_org_numbers",
]

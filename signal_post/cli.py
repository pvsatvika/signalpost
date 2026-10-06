"""
Command-line interface for Signalpost.
"""

import argparse
import json
import sys
from typing import Optional, List

from signal_post.client import BrregClient
from signal_post.exceptions import SignalpostError
from signal_post.models import NormalizedCompanyProfile
from signal_post.storage import (
    get_connection,
    init_db,
    save_company_profile,
    get_company_profile_with_evidence,
    get_change_history,
)
from signal_post.refresh import refresh_company, RefreshResult, FactChangeItem
from signal_post.budget import RequestBudgetTracker
from signal_post.discovery import discover_organizations
from signal_post.collection import collect_queued_profiles, get_collection_status
from signal_post.bulk import (
    download_bulk_dataset,
    import_bulk_profiles,
    generate_bootstrap_manifest,
    import_bulk_from_manifest,
    validate_database_integrity,
)
from pathlib import Path
from signal_post.runner import run_evaluation, load_input_org_numbers


def print_human_summary(profile: NormalizedCompanyProfile) -> None:
    """Format and print a human-readable summary of the normalized company profile."""
    print("=" * 65)
    print(f" COMPANY PROFILE: {profile.name}")
    print("=" * 65)
    print(f" Organization Number : {profile.org_number}")
    
    if profile.parent_org_number:
        print(f" Parent Org Number   : {profile.parent_org_number}")

    if profile.organization_form:
        code = profile.organization_form.kode or "N/A"
        desc = profile.organization_form.beskrivelse or ""
        print(f" Organization Form   : {code} ({desc})")
        
    if profile.primary_industry:
        code = profile.primary_industry.kode or "N/A"
        desc = profile.primary_industry.beskrivelse or ""
        print(f" Primary Industry    : {code} - {desc}")

    if profile.secondary_industry:
        code = profile.secondary_industry.kode or "N/A"
        desc = profile.secondary_industry.beskrivelse or ""
        print(f" Secondary Industry  : {code} - {desc}")
        
    if profile.num_employees is not None:
        print(f" Employees           : {profile.num_employees}")
    else:
        print(f" Employees           : Not registered")

    if profile.registration_date:
        print(f" Registration Date   : {profile.registration_date}")

    if profile.foundation_date:
        print(f" Foundation Date     : {profile.foundation_date}")

    if profile.business_address and profile.business_address.adresse:
        addr = ", ".join(profile.business_address.adresse)
        post = f"{profile.business_address.postnummer or ''} {profile.business_address.poststed or ''}".strip()
        country = profile.business_address.land or ""
        print(f" Business Address    : {addr}, {post} {country}".strip())

    if profile.postal_address and profile.postal_address.adresse:
        addr = ", ".join(profile.postal_address.adresse)
        post = f"{profile.postal_address.postnummer or ''} {profile.postal_address.poststed or ''}".strip()
        print(f" Postal Address      : {addr}, {post}".strip())

    if profile.website:
        print(f" Website             : {profile.website}")
    if profile.email:
        print(f" Email               : {profile.email}")
    if profile.phone:
        print(f" Phone               : {profile.phone}")

    status_flags = []
    if profile.registered_in_foretaksregisteret:
        status_flags.append("Foretaksregisteret")
    if profile.registered_in_mvaregisteret:
        status_flags.append("MVA Register")
    if profile.is_in_group:
        status_flags.append("Corporate Group")
    if profile.is_bankrupt is True:
        status_flags.append("BANKRUPT")
    if profile.under_liquidation is True:
        status_flags.append("LIQUIDATION")
    if profile.under_compulsory_dissolution is True:
        status_flags.append("DISSOLUTION")

    if status_flags:
        print(f" Registry Status     : {', '.join(status_flags)}")

    if profile.last_submitted_annual_accounts:
        print(f" Last Accounts Year  : {profile.last_submitted_annual_accounts}")

    if profile.historical_names:
        names_str = ", ".join(h.navn for h in profile.historical_names)
        print(f" Historical Names    : {names_str}")

    print("=" * 65)


def print_refresh_report(result: RefreshResult) -> None:
    """Print human-readable change report following a profile refresh."""
    print("=" * 70)
    print(f" COMPANY PROFILE REFRESH REPORT: {result.company_name or 'N/A'}")
    print("=" * 70)
    print(f" Organization Number : {result.org_number}")
    print(f" Status              : {result.status.upper()}")
    print(f" Retrieval Time      : {result.retrieved_at}")
    print(f" Source              : {result.source_name} ({result.source_id})")
    print(f" Exact Source URL    : {result.exact_source_url}")

    if result.status == "failed":
        print(f" Error Details       : {result.error_message}")
        print("=" * 70)
        return

    counts = result.summary_counts
    count_str = ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0) or "none"
    print(f" Summary Counts      : {count_str}")
    print("-" * 70)

    notable_changes = [c for c in result.changes if c.change_type != "unchanged"]

    if not notable_changes:
        print(" Result: No profile changes detected. All active facts remain unchanged.")
    else:
        print(" Detected Fact Changes & Explanations:")
        for item in notable_changes:
            prefix = f"[{item.change_type.upper()}]"
            print(f"\n  {prefix:<12} Fact: {item.fact_key}")
            print(f"    Explanation: {item.explanation}")
            if item.previous_value is not None:
                print(f"    Previous   : {item.previous_value}")
            if item.new_value is not None:
                print(f"    New Value  : {item.new_value}")

    print("=" * 70)


def inspect_stored_facts(db_path: str, org_number: str) -> None:
    """Print stored facts and evidence for a company from SQLite database."""
    conn = get_connection(db_path)
    data = get_company_profile_with_evidence(conn, org_number)
    conn.close()

    if not data:
        print(f"No stored profile found in database '{db_path}' for org number '{org_number}'.")
        return

    comp = data["company"]
    facts = data["facts"]

    print("=" * 70)
    print(f" STORED FACTS & EVIDENCE FOR: {comp['name']} ({comp['org_number']})")
    print(f" Database: {db_path} | First Seen: {comp['first_seen_at']}")
    print("=" * 70)

    for f in facts:
        print(f"\n[Fact #{f['fact_id']}] Key: {f['fact_key']} | Status: {f['verification_status']}")
        print(f"  Value: {f['fact_value']}")
        print(f"  Observed: {f['first_observed_at']} -> {f['last_observed_at']}")

        evidence_list = f.get("evidence", [])
        print(f"  Evidence ({len(evidence_list)} record(s)):")
        for ev in evidence_list:
            print(f"    - Ev #{ev['evidence_id']} | Source: {ev['source_id']} | Retrieved: {ev['retrieved_at']}")
            print(f"      URL: {ev['exact_source_url']}")
    print("=" * 70)


def inspect_stored_history(db_path: str, org_number: str) -> None:
    """Print stored change history for a company from SQLite database."""
    conn = get_connection(db_path)
    history = get_change_history(conn, org_number)
    conn.close()

    if not history:
        print(f"No change history records found in database '{db_path}' for org number '{org_number}'.")
        return

    print("=" * 70)
    print(f" CHANGE HISTORY FOR ORG NUMBER: {org_number}")
    print(f" Database: {db_path}")
    print("=" * 70)

    for h in history:
        print(f"\n[Change #{h['change_id']}] Key: {h['fact_key']} | Type: {h['change_type']} | Date: {h['changed_at']}")
        if h["previous_value"] is not None:
            print(f"  Old Value : {h['previous_value']}")
        print(f"  New Value : {h['new_value']}")
        if h["reason"]:
            print(f"  Reason    : {h['reason']}")
    print("=" * 70)


def print_collection_status(status_info: Dict[str, Any], db_path: str) -> None:
    """Print human-readable summary of collection and queue status."""
    print("=" * 70)
    print(f" SIGNALPOST DISCOVERY & COLLECTION STATUS")
    print(f" Database: {db_path}")
    print("=" * 70)
    print(f" Total Organizations Queued : {status_info['total_queued']}")
    print(f" Pending Collection         : {status_info['pending_count']}")
    print(f" Completed Collection       : {status_info['completed_count']}")
    print(f" Failed Collection          : {status_info['failed_count']}")
    print(f" Stored Companies in DB     : {status_info['stored_companies_count']}")
    print(f" Requests Used Today        : {status_info['requests_today']}")
    if status_info['remaining_budget'] is not None:
        print(f" Remaining Daily Budget     : {status_info['remaining_budget']}")
    print("=" * 70)


def main(args: Optional[List[str]] = None) -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="signalpost",
        description="Signalpost - Discover, retrieve, normalize, store, and refresh Norwegian company profiles.",
    )
    parser.add_argument(
        "org_number",
        nargs="?",
        default=None,
        help="Optional 9-digit Norwegian organization number (organisasjonsnummer)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output profile, report, or status as JSON",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw registry response in JSON output",
    )
    parser.add_argument(
        "--db",
        type=str,
        help="SQLite database file path for storing/inspecting company profiles & evidence",
    )
    parser.add_argument(
        "--store",
        action="store_true",
        help="Store fetched company profile and evidence to SQLite database (requires --db)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Safely refresh company profile, detect changes, store updates, and report explanations (requires --db)",
    )
    parser.add_argument(
        "--inspect-facts",
        action="store_true",
        help="Inspect stored active facts and evidence from SQLite database (requires --db)",
    )
    parser.add_argument(
        "--inspect-history",
        action="store_true",
        help="Inspect stored change history from SQLite database (requires --db)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover organizations from Brønnøysund API and queue org numbers into SQLite",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Collect and normalize profiles for queued organization numbers",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume collection including pending and failed queue items",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Inspect discovery queue and collection status metrics in SQLite",
    )
    parser.add_argument(
        "--bulk-download",
        action="store_true",
        help="Download official Brønnøysund bulk open data dataset file using request budget tracker",
    )
    parser.add_argument(
        "--bulk-import",
        type=str,
        nargs="?",
        const="enheter_alle.json.gz",
        default=None,
        help="Import profiles from local official bulk dataset file into SQLite (default file: enheter_alle.json.gz)",
    )
    parser.add_argument(
        "--generate-manifest",
        action="store_true",
        help="Generate reproducible 1,000-profile bootstrap selection manifest JSON (data/bootstrap_manifest.json)",
    )
    parser.add_argument(
        "--import-manifest",
        action="store_true",
        help="Import profiles specified in bootstrap manifest JSON from local bulk file into SQLite",
    )
    parser.add_argument(
        "--manifest",
        type=str,
        default="data/bootstrap_manifest.json",
        help="Bootstrap manifest file path (default: data/bootstrap_manifest.json)",
    )
    parser.add_argument(
        "--validate-db",
        action="store_true",
        help="Run programmatic integrity and evidence verification checks on SQLite database",
    )
    parser.add_argument(
        "--file",
        type=str,
        default="enheter_alle_test.json.gz",
        help="Local file path for bulk download, import, or manifest generation (default: enheter_alle_test.json.gz)",
    )
    parser.add_argument(
        "--filter-org-form",
        type=str,
        default="AS",
        help="Organization form filter for discovery/bulk import (e.g. 'AS', 'ASA', 'ENK'). Default: 'AS'",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Limit for discovery items, bulk import, or collection requests (default: 1000)",
    )
    parser.add_argument(
        "--request-budget",
        type=int,
        default=100,
        help="Max outbound HTTP request budget for session/day (default: 100)",
    )
    parser.add_argument(
        "--run",
        "-r",
        type=str,
        default=None,
        help="Evaluator execution mode: process list of organization numbers from JSON file, text file, or '-' for stdin",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output JSON result file path for evaluator run (or '-' for stdout)",
    )
    parser.add_argument(
        "--no-live",
        "--offline",
        action="store_true",
        help="Disable live API refresh during evaluator run (use SQLite local cache)",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="registry,roles,accounts",
        help="Comma-separated list of data sources to enable (e.g. 'registry,roles,accounts'). Default: 'registry,roles,accounts'",
    )

    parsed = parser.parse_args(args)

    if (parsed.store or parsed.refresh or parsed.inspect_facts or parsed.inspect_history or
            parsed.discover or parsed.collect or parsed.resume or parsed.status or
            parsed.bulk_download or parsed.bulk_import) and not parsed.db:
        parsed.db = "signalpost.db"

    client = BrregClient()

    try:
        if parsed.run is not None:
            db_file = parsed.db or "signalpost.db"
            live_refresh = not parsed.no_live
            sources_list = [s.strip().lower() for s in (parsed.sources or "registry,roles,accounts").split(",") if s.strip()]
            include_roles = "roles" in sources_list
            include_finanstilsynet = "finanstilsynet" in sources_list
            include_accounts = "accounts" in sources_list
            include_fullmakt = "fullmakt" in sources_list
            include_subentities = "subentities" in sources_list

            try:
                org_numbers = load_input_org_numbers(parsed.run)
            except Exception as e:
                print(f"Error loading input organization numbers: {e}", file=sys.stderr)
                sys.exit(1)

            out_dest = parsed.output or "-"
            out_mode = "stdout" if out_dest == "-" else "file"

            evaluation_report = run_evaluation(
                org_numbers=org_numbers,
                db_path=db_file,
                live_refresh=live_refresh,
                include_roles=include_roles,
                include_finanstilsynet=include_finanstilsynet,
                include_accounts=include_accounts,
                include_fullmakt=include_fullmakt,
                include_subentities=include_subentities,
                client=client,
                max_requests_limit=parsed.request_budget,
                input_source_identifier=parsed.run,
                output_mode=out_mode,
            )
            json_output = json.dumps(evaluation_report, indent=2, ensure_ascii=False)

            if out_dest == "-":
                print(json_output)
            else:
                out_p = Path(out_dest)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(json_output, encoding="utf-8")

            # Terminal summary output (to stderr if stdout was used for JSON output)
            metrics = evaluation_report["run_metrics"]
            out_stream = sys.stderr if out_dest == "-" else sys.stdout
            print("=" * 70, file=out_stream)
            print(" EVALUATION RUN COMPLETED", file=out_stream)
            print("=" * 70, file=out_stream)
            print(f" Database            : {db_file}", file=out_stream)
            print(f" Requested Orgs      : {metrics['requested_count']}", file=out_stream)
            print(f" Successfully Processed: {metrics['success_count']}", file=out_stream)
            print(f" Failed              : {metrics['failed_count']}", file=out_stream)
            print(f" Served From Local   : {metrics['served_from_local_count']}", file=out_stream)
            print(f" Live Refreshed      : {metrics['live_refreshed_count']}", file=out_stream)
            print(f" Outbound Requests   : {metrics['total_outbound_requests']}", file=out_stream)
            print(f" Wall-Clock Time     : {metrics['elapsed_seconds']}s", file=out_stream)
            print(f" Avg Time / Company  : {metrics['average_seconds_per_company']}s", file=out_stream)
            print(f" Estimated API Cost  : {metrics['estimated_external_api_cost']}", file=out_stream)
            print("=" * 70, file=out_stream)
            return

        # Phase 4: Discovery & Collection workflow actions
        if parsed.status:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            init_db(conn)
            tracker = RequestBudgetTracker(conn, budget_limit=parsed.request_budget)
            status_info = get_collection_status(conn, budget_tracker=tracker)
            conn.close()

            if parsed.json:
                print(json.dumps(status_info, indent=2, ensure_ascii=False))
            else:
                print_collection_status(status_info, db_file)
            return

        if parsed.discover:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            init_db(conn)
            tracker = RequestBudgetTracker(conn, budget_limit=parsed.request_budget)

            summary = discover_organizations(
                conn=conn,
                client=client,
                budget_tracker=tracker,
                filter_org_form=parsed.filter_org_form,
                limit=parsed.limit,
            )
            conn.close()

            if parsed.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" DISCOVERY WORKFLOW COMPLETED")
                print("=" * 70)
                print(f" Discovered New Orgs : {summary['discovered_new']}")
                print(f" Already Queued       : {summary['already_queued']}")
                print(f" Total Queued         : {summary['total_in_queue']}")
                print(f" Pending Queued       : {summary['pending_in_queue']}")
                print(f" Pages Fetched        : {summary['pages_fetched']}")
                print(f" Requests Made        : {summary['requests_made']}")
                print(f" Stopped Reason       : {summary['stopped_reason']}")
                print("=" * 70)
            return

        if parsed.collect or parsed.resume:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            init_db(conn)
            tracker = RequestBudgetTracker(conn, budget_limit=parsed.request_budget)

            summary = collect_queued_profiles(
                conn=conn,
                client=client,
                budget_tracker=tracker,
                limit=parsed.limit,
                resume_failed=parsed.resume,
            )
            conn.close()

            if parsed.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" COLLECTION WORKFLOW COMPLETED")
                print("=" * 70)
                print(f" Attempted Profiles  : {summary['attempted']}")
                print(f" Completed           : {summary['completed']}")
                print(f" Failed              : {summary['failed']}")
                print(f" Requests Made       : {summary['requests_made']}")
                print(f" Stopped Reason      : {summary['stopped_reason']}")
                print(f" Total Queue Items   : {summary['total_queued']}")
                print(f" Remaining Pending   : {summary['pending_in_queue']}")
                print(f" Stored Companies DB : {summary['stored_companies_count']}")
                print("=" * 70)
            return

        if parsed.bulk_download:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            init_db(conn)
            tracker = RequestBudgetTracker(conn, budget_limit=parsed.request_budget)
            target_file = parsed.file or "enheter_alle.json.gz"

            summary = download_bulk_dataset(output_path=target_file, budget_tracker=tracker)
            conn.close()

            if parsed.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" BULK DATASET DOWNLOAD COMPLETED")
                print("=" * 70)
                print(f" Target File Path    : {summary['file_path']}")
                print(f" File Size Bytes     : {summary['file_size_bytes']}")
                print(f" Last Modified       : {summary['last_modified'] or 'N/A'}")
                print(f" Outbound Requests   : {summary['requests_made']}")
                print("=" * 70)
            return

        if parsed.bulk_import:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            init_db(conn)
            target_file = parsed.bulk_import if parsed.bulk_import != "enheter_alle.json.gz" else (parsed.file or "enheter_alle.json.gz")

            summary = import_bulk_profiles(
                conn=conn,
                file_path=target_file,
                limit=parsed.limit,
                filter_org_form=parsed.filter_org_form,
            )
            conn.close()

            if parsed.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" BULK PROFILES IMPORT COMPLETED")
                print("=" * 70)
                print(f" Source File Path    : {target_file}")
                print(f" Imported New Orgs   : {summary['imported_count']}")
                print(f" Already Stored Orgs : {summary['already_stored_count']}")
                print(f" Invalid Skipped     : {summary['invalid_skipped']}")
                print(f" Malformed Skipped   : {summary['malformed_skipped']}")
                print(f" Total Facts Saved   : {summary['total_facts_saved']}")
                print(f" Total Evidence Saved: {summary['total_evidence_saved']}")
                print(f" Stopped Reason      : {summary['stopped_reason']}")
                print("=" * 70)
            return

        if parsed.generate_manifest:
            target_file = parsed.file or "enheter_alle_test.json.gz"
            manifest_path = parsed.manifest or "data/bootstrap_manifest.json"

            manifest = generate_bootstrap_manifest(
                file_path=target_file,
                output_manifest_path=manifest_path,
                target_count=parsed.limit,
            )

            if parsed.json:
                print(json.dumps(manifest, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" BOOTSTRAP SELECTION MANIFEST GENERATED")
                print("=" * 70)
                print(f" Manifest Path       : {manifest_path}")
                print(f" Bulk Dataset File   : {manifest['local_bulk_filename']}")
                print(f" Requested Count     : {manifest['requested_count']}")
                print(f" Actual Count        : {manifest['actual_count']}")
                print(f" Org Form Counts     : {manifest['org_form_distribution']}")
                print("=" * 70)
            return

        if parsed.import_manifest:
            db_file = parsed.db or "signalpost_1000.db"
            conn = get_connection(db_file)
            manifest_path = parsed.manifest or "data/bootstrap_manifest.json"
            target_file = parsed.file or "enheter_alle_test.json.gz"

            summary = import_bulk_from_manifest(
                conn=conn,
                manifest_path=manifest_path,
                file_path=target_file,
            )
            conn.close()

            if parsed.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" BOOTSTRAP MANIFEST IMPORT COMPLETED")
                print("=" * 70)
                print(f" Database            : {db_file}")
                print(f" Manifest Path       : {manifest_path}")
                print(f" Imported New Orgs   : {summary['imported_count']}")
                print(f" Already Stored Orgs : {summary['already_stored_count']}")
                print(f" Total Facts Saved   : {summary['total_facts_saved']}")
                print(f" Total Evidence Saved: {summary['total_evidence_saved']}")
                print("=" * 70)
            return

        if parsed.validate_db:
            db_file = parsed.db or "signalpost_1000.db"
            conn = get_connection(db_file)
            report = validate_database_integrity(conn, expected_company_count=parsed.limit)
            conn.close()

            if parsed.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(" DATABASE INTEGRITY VALIDATION REPORT")
                print("=" * 70)
                print(f" Database            : {db_file}")
                print(f" Integrity Check     : {'PASSED (ok)' if report['integrity_check_passed'] else 'FAILED'}")
                fk_violations = report['foreign_key_violations']
                print(f" Foreign Key Check   : {'PASSED (0 violations)' if fk_violations == 0 else f'FAILED ({fk_violations} violations)'}")
                print(f" Total Companies     : {report['total_companies']} (Expected: {report['expected_companies']})")
                print(f" Distinct Org Numbers: {report['distinct_org_numbers']}")
                print(f" Invalid Org Numbers : {report['invalid_org_numbers_found']}")
                print(f" Total Active Facts  : {report['total_active_facts']}")
                print(f" Total Evidence Rows : {report['total_evidence_rows']}")
                print(f" Min Facts/Company   : {report['min_facts_per_company']}")
                print(f" Max Facts/Company   : {report['max_facts_per_company']}")
                print(f" Avg Facts/Company   : {report['avg_facts_per_company']}")
                print(f" Org Form Counts     : {report['org_form_distribution']}")
                print("=" * 70)
            return

        # Handle organization-number specific options
        if not parsed.org_number:
            parser.error("An organization number is required unless --run, --discover, --collect, --resume, --status, --bulk-download, --bulk-import, --generate-manifest, --import-manifest, or --validate-db is specified.")

        if parsed.refresh:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            result = refresh_company(conn, parsed.org_number, client=client)
            conn.close()

            if result.status == "failed":
                if parsed.json:
                    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
                else:
                    print(f"Error: Refresh failed for organization number '{parsed.org_number}': {result.error_message}", file=sys.stderr)
                sys.exit(1)

            if parsed.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                print_refresh_report(result)
            return

        if parsed.inspect_facts and not parsed.store:
            inspect_stored_facts(parsed.db, parsed.org_number)
            return

        if parsed.inspect_history and not parsed.store:
            inspect_stored_history(parsed.db, parsed.org_number)
            return

        profile = client.get_company_profile(parsed.org_number)

        if parsed.db or parsed.store:
            db_file = parsed.db or "signalpost.db"
            conn = get_connection(db_file)
            init_db(conn)
            save_company_profile(conn, profile)
            conn.close()
            print(f"[Signalpost] Stored company '{profile.name}' ({profile.org_number}) and facts to SQLite: '{db_file}'\n")

        if parsed.inspect_facts:
            inspect_stored_facts(parsed.db, parsed.org_number)
        elif parsed.inspect_history:
            inspect_stored_history(parsed.db, parsed.org_number)
        elif parsed.json:
            dict_repr = profile.model_dump()
            if not parsed.include_raw:
                dict_repr.pop("raw_data", None)
            print(json.dumps(dict_repr, indent=2, ensure_ascii=False))
        else:
            print_human_summary(profile)

    except SignalpostError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

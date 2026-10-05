"""
Pydantic data models for normalized company profiles.
"""

import copy
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Address(BaseModel):
    """Postal or business address."""
    adresse: List[str] = Field(default_factory=list)
    postnummer: Optional[str] = None
    poststed: Optional[str] = None
    kommune: Optional[str] = None
    kommunenummer: Optional[str] = None
    land: Optional[str] = None
    landkode: Optional[str] = None


class IndustryCode(BaseModel):
    """Næringskode classification."""
    kode: Optional[str] = None
    beskrivelse: Optional[str] = None


class OrganizationForm(BaseModel):
    """Organisasjonsform description (e.g., AS, ASA, ORGL)."""
    kode: Optional[str] = None
    beskrivelse: Optional[str] = None


class CapitalInfo(BaseModel):
    """Share capital details when available."""
    type: Optional[str] = None
    belop: Optional[float] = None
    valuta: Optional[str] = None
    antallAksjer: Optional[int] = None
    innfortDato: Optional[str] = None


class HistoricalName(BaseModel):
    """Historical company name entry."""
    navn: str
    fraDato: Optional[str] = None
    tilDato: Optional[str] = None


class NormalizedCompanyProfile(BaseModel):
    """
    Normalized company profile extracted from official Brønnøysund Registry record.
    Preserves raw response JSON in `raw_data`.
    """
    org_number: str
    name: str
    organization_form: Optional[OrganizationForm] = None
    primary_industry: Optional[IndustryCode] = None
    secondary_industry: Optional[IndustryCode] = None
    tertiary_industry: Optional[IndustryCode] = None
    num_employees: Optional[int] = None
    has_registered_employees: Optional[bool] = None
    registration_date: Optional[str] = None
    foundation_date: Optional[str] = None
    parent_org_number: Optional[str] = None
    historical_names: List[HistoricalName] = Field(default_factory=list)
    business_address: Optional[Address] = None
    postal_address: Optional[Address] = None
    website: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    registered_in_foretaksregisteret: Optional[bool] = None
    registered_in_mvaregisteret: Optional[bool] = None
    registered_in_frivillighetsregisteret: Optional[bool] = None
    registered_in_stiftelsesregisteret: Optional[bool] = None
    registered_in_partiregisteret: Optional[bool] = None
    is_in_group: Optional[bool] = None
    under_liquidation: Optional[bool] = None
    under_compulsory_dissolution: Optional[bool] = None
    is_bankrupt: Optional[bool] = None
    last_submitted_annual_accounts: Optional[str] = None
    capital: Optional[CapitalInfo] = None
    raw_data: Dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_raw_dict(cls, raw: Dict[str, Any]) -> "NormalizedCompanyProfile":
        """
        Safely construct a normalized company profile from raw Brønnøysund JSON data.
        Handles optional and missing fields cleanly.
        """
        org_form_raw = raw.get("organisasjonsform")
        org_form = OrganizationForm(**org_form_raw) if isinstance(org_form_raw, dict) else None

        ind1_raw = raw.get("naeringskode1")
        primary_industry = IndustryCode(**ind1_raw) if isinstance(ind1_raw, dict) else None

        ind2_raw = raw.get("naeringskode2")
        secondary_industry = IndustryCode(**ind2_raw) if isinstance(ind2_raw, dict) else None

        ind3_raw = raw.get("naeringskode3")
        tertiary_industry = IndustryCode(**ind3_raw) if isinstance(ind3_raw, dict) else None

        bus_addr_raw = raw.get("forretningsadresse")
        bus_addr = Address(**bus_addr_raw) if isinstance(bus_addr_raw, dict) else None

        post_addr_raw = raw.get("postadresse")
        post_addr = Address(**post_addr_raw) if isinstance(post_addr_raw, dict) else None

        cap_raw = raw.get("kapital")
        capital = CapitalInfo(**cap_raw) if isinstance(cap_raw, dict) else None

        hist_names_raw = raw.get("historiskeNavn", [])
        hist_names = [
            HistoricalName(**item) for item in hist_names_raw if isinstance(item, dict) and "navn" in item
        ] if isinstance(hist_names_raw, list) else []

        return cls(
            org_number=str(raw.get("organisasjonsnummer", "")),
            name=raw.get("navn", ""),
            organization_form=org_form,
            primary_industry=primary_industry,
            secondary_industry=secondary_industry,
            tertiary_industry=tertiary_industry,
            num_employees=raw.get("antallAnsatte"),
            has_registered_employees=raw.get("harRegistrertAntallAnsatte"),
            registration_date=raw.get("registreringsdatoEnhetsregisteret"),
            foundation_date=raw.get("stiftelsesdato"),
            parent_org_number=raw.get("overordnetEnhet"),
            historical_names=hist_names,
            business_address=bus_addr,
            postal_address=post_addr,
            website=raw.get("hjemmeside"),
            email=raw.get("epostadresse"),
            phone=raw.get("telefon"),
            registered_in_foretaksregisteret=raw.get("registrertIForetaksregisteret"),
            registered_in_mvaregisteret=raw.get("registrertIMvaregisteret"),
            registered_in_frivillighetsregisteret=raw.get("registrertIFrivillighetsregisteret"),
            registered_in_stiftelsesregisteret=raw.get("registrertIStiftelsesregisteret"),
            registered_in_partiregisteret=raw.get("registrertIPartiregisteret"),
            is_in_group=raw.get("erIKonsern"),
            under_liquidation=raw.get("underAvvikling"),
            under_compulsory_dissolution=raw.get("underTvangsavviklingEllerTvangsopplosning"),
            is_bankrupt=raw.get("konkurs"),
            last_submitted_annual_accounts=raw.get("sisteInnsendteAarsregnskap"),
            capital=capital,
            raw_data=copy.deepcopy(raw),
        )

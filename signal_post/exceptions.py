"""
Custom exceptions for Signalpost registry operations.
"""


class SignalpostError(Exception):
    """Base exception for all Signalpost errors."""
    pass


class InvalidOrgNumberError(SignalpostError):
    """Raised when an organization number is not a valid 9-digit string."""
    pass


class CompanyNotFoundError(SignalpostError):
    """Raised when a company is not found in the Brønnøysund Registry (HTTP 404)."""
    pass


class RegistryClientError(SignalpostError):
    """Raised when request to registry fails (network, timeout, non-200, invalid JSON)."""
    pass


class DataMismatchError(RegistryClientError):
    """Raised when returned registry response org number does not match requested org number."""
    pass

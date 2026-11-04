"""KIS 可預期的 Domain/Application 例外階層。"""


class DomainError(Exception):
    """Base exception for expected domain failures."""


class ValidationError(DomainError):
    """Input violates a domain invariant."""


class EntityNotFoundError(DomainError):
    """Requested aggregate does not exist."""


class ConflictError(DomainError):
    """Operation conflicts with the current aggregate state."""


class ServiceNotReadyError(DomainError):
    """A required infrastructure dependency is not ready."""


class CapacityExceededError(DomainError):
    """Request exceeds a configured service capacity."""

"""Errors deliberately distinguish rejection from an ambiguous mutation."""


class UpgradeError(RuntimeError):
    """The upgrade cannot safely advance."""


class Conflict(UpgradeError):
    """A version or identity precondition did not match."""


class UnknownOutcome(UpgradeError):
    """A mutation may have executed; inspection is required before retry."""


class Blocked(UpgradeError):
    """An explicit prerequisite has not been met."""

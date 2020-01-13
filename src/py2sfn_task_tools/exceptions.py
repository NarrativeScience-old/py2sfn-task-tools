"""Contains exception classes for the SFN tools library"""


class StateDataClientError(Exception):
    """Base class for state data client exceptions"""

    pass


class NoItemFound(StateDataClientError):
    """Raised when no item could be found in the state data table"""

    pass

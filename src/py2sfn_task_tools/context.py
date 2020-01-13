"""Contains a data class holding common clients for SFN tasks"""
from typing import Callable, NamedTuple

from .state_data_client import StateDataClient


class TaskContext(NamedTuple):
    """Data class holding common clients for SFN tasks.

    This will be passed as the ``context`` argument to each task's ``run`` method.
    """

    state_data_client: StateDataClient = None
    stop_execution: Callable = None

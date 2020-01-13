"""Contains a function for stopping a running execution"""
from sfn_workflow_client.workflow import Workflow


async def stop_execution(
    workflow_name: str, execution_id: str, error: str = None, cause: str = None
) -> None:
    """Helper function for stopping a running execution.

    This will send an API call to stop the execution then poll until its status
    converges to aborted.

    Args:
        workflow_name: Workflow name
        execution_id: Execution ID to stop
        error: The error code of why the execution should be stopped
        cause: A more detailed explanation of the cause of the stop

    """
    workflow = Workflow(workflow_name)
    await workflow.executions.create(execution_id=execution_id).stop(
        error=error, cause=cause
    )

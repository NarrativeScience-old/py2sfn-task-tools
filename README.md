# py2sfn-task-tools

[![](https://img.shields.io/pypi/v/py2sfn-task-tools.svg)](https://pypi.org/pypi/py2sfn-task-tools/) [![License](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)](https://opensource.org/licenses/BSD-3-Clause)

Tools for tasks embedded in an [AWS Step Functions](https://aws.amazon.com/step-functions/) state machine. This is a helper library for [py2sfn](https://github.com/NarrativeScience/py2sfn).

Features:

- Offload state data to DynamoDB/S3 instead of storing data in the *very* constrained state machine input data object
- Cancel the currently executing workflow

Table of Contents:

- [Installation](#installation)
- [Guide](#guide)
  - [Stopping the execution](#stopping-the-execution)
  - [Working with the State Data Client](#working-with-the-state-data-client)
- [Development](#development)

## Installation

py2sfn-task-tools requires Python 3.6 or above. It should be installed in a [py2sfn task entry point](https://github.com/NarrativeScience/py2sfn#task-entry-points).

```bash
pip install py2sfn-task-tools
```

## Guide

Once the py2sfn-task-tools library is installed, a `Context` should be created and passed to the tasks. Each py2sfn task will then have a `context` object to work with.

### Stopping the execution

If you need to stop/cancel/abort the execution from within a task, you can use the  `context.stop_execution` method within your task's `run` method. A common use case is if you need to check the value of a feature flag at the beginning of the execution and abort if it's false. For example:

```python
if not some_condition:
    return await context.stop_execution()
```

You can provide extra detail by passing `error` and `cause` keyword arguments to the `stop_execution` method. The `error` is a short string like a code or enum value whereas `cause` is a longer description.

### Working with the State Data Client

One of the stated Step Functions best practices is to avoid passing large payloads between states; the input data limit is only 32K characters. To get around this, you can choose to store data from your task code in a DynamoDB table. With DynamoDB, we have an item limit of 400KB to work with. When you put items into the table you receive a pointer to the DynamoDB item which you can return from your task so it gets includes in the input data object. From there, since the pointer is in the `data` dict, you can reload the stored data in a downstream task. This library's `StateDataClient` class provides methods for putting and getting items from this DynamoDB table. It's available in your task's `run` method as `context.state_data_client`.

The client methods are split between "local" and "global" variants. Local methods operate on items stored within the project whereas global methods can operate on items that were stored from any project. Global methods require a fully-specified partition key (primary key, contains the execution ID) and table name to locate the item whereas local methods only need a simple key because the partition key and table name can be infered from the project automatically. The `put_*` methods return a dict with metadata about the location of the item, including the `key`, `partition_key`, and `table_name`. If you return this metadata object from a task, it will get put on the `data` object and you can call a `get_*` method later in the state machine.

Many methods also accept an optional `index` argument. This argument needs to be provided when getting/putting an item that was originally stored as part of a `put_items` or `put_global_items` call. Providing the `index` is usually only done within a map iteration task.

Below are a few of the more common methods:

#### `put_item`/`put_items`

The `put_item` method puts an item in the state store. It takes `key`, `data`, and `index` arguments. For example:

```python
context.state_data_client.put_item("characters", {"name": "jerry"})
context.state_data_client.put_item("characters", {"name": "elaine"}, index=24)
```

Note that the item at the given array index doesn't actually have to exist in the table before you call `put_item`. However, if it doesn't exist then you may have a fan-out logic bug upstream in your state machine.

The `put_items` method puts an entire list of items into the state store. Each item will be stored separately under its corresponding array index. For example:

```python
context.state_data_client.put_items("characters", [{"name": "jerry"}, {"name": "elaine"}])
```

#### `get_item`

The `get_item` method gets the data attribute from an item in the state store. It takes `key` and `index` arguments. For example:

```python
context.state_data_client.get_item("characters")  # -> {"name": "jerry"}
context.state_data_client.get_item("characters", index=24)  # -> {"name": "elaine"}
```

#### `get_item_for_map_iteration`/`get_global_item_for_map_iteration`

The `get_item_for_map_iteration` method gets the data attribute from an item in the state store using the `event` object. This method only works when called within a map iterator task. For example, if the `put_items` example above was called in a task, and its value was given to a map state to fan out, we can use the `get_item_for_map_iteration` method within our iterator task to fetch each item:

```python
# Iteration 0:
context.state_data_client.get_item_for_map_iteration(event)  # -> {"name": "jerry"}
# Iteration 1:
context.state_data_client.get_item_for_map_iteration(event)  # -> {"name": "elaine"}
```

This works because the map iterator state machine receives an input data object with the schema:

```json
{
  "items_result_table_name": "<DynamoDB table for the project>",
  "items_result_partition_key": "<execution ID>:characters",
  "items_result_key": "characters",
  "context_index": "<array index>",
  "context_value.$": "1"
}
```

The `get_item_for_map_iteration` is a helper method that uses that input to locate the right item. The `get_global_item_for_map_iteration` method has the same signature. It should be called when you know that the array used to fan out could have come from another project (e.g. the map state is the first state in a state machine triggered by a subscription).

## Development

To run functional tests, you need an AWS IAM account with permissions to:

- Create/update/delete a DynamoDB table
- Create/update/delete an S3 bucket

Set the following environment variables:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_DEFAULT_REGION`

To run tests:

```bash
tox
```

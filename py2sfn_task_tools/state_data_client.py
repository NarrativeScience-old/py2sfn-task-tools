"""Contains the state data client"""
import datetime
import json
import os
from typing import Any, Dict, List

import backoff
import boto3
from botocore.exceptions import ClientError

from .exceptions import NoItemFound

dynamodb = boto3.client(
    "dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL")
)
s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL"))

#: If an item's data is larger than this threshold it will be stored in S3 instead of
#: DynamoDB. The item limit is 400KB but we'll leave room for other attributes.
#: See: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Limits.html#limits-items
ITEM_SIZE_THRESHOLD_BYTES = 390_000


def _giveup_client_error(exc: Exception) -> bool:
    """Handler for the backoff decorator to stop retrying boto3 client errors.

    Args:
        exc: Exception raised by a function decorated with ``backoff.on_exception``

    Returns:
        True if the status code is a client error (HTTP 4xx), False otherwise

    """
    try:
        return 400 <= exc.response["ResponseMetadata"]["HTTPStatusCode"] < 500
    except (AttributeError, KeyError):
        return False


class StateDataClient:
    """State Machine data client for persisting intermediate state data.

    This client is a thin wrapper around DynamoDB. It provides a key-value store for
    tasks to use to store data remotely instead of passing it directly to downstream
    states in the state machine input data object. This is handy when the state data is
    larger than 32K characters (the AWS limit).

    """

    def __init__(
        self,
        default_table_name: str,
        namespace: str,
        ttl_days: int = 7,
        s3_bucket: str = os.environ.get("SFN_DATA_S3_BUCKET"),
    ) -> None:
        """
        Args:
            default_table_name: DynamoDB table name for storing key-values that are
                scoped to an execution. This table must already exist beforehand. Its
                schema should define:
                * **partition_key** -- string
                * **sort_key** -- string
                * **data** -- string
                * **expires_at** -- int
            namespace: A namespace represents a collection of related items. The
                namespace will be used as the prefix in the table's partition key, and
                therefore will be included in all calls to DynamoDB that reference
                local items. This should generally be the state machine execution
                name (ID).
            ttl_days: Number of days that the stored item will be kept. DynamoDB will
                expire items after the TTL.
            s3_bucket: S3 bucket name where item data is stored if it's larger than the
                ITEM_SIZE_THRESHOLD_BYTES constant.

        """
        self.default_table_name = default_table_name
        self.namespace = namespace
        self.ttl_days = ttl_days
        self.s3_bucket = boto3.resource(
            "s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL")
        ).Bucket(s3_bucket)

    def table(self, table_name: str) -> "dynamodb.Table":
        """Helper method to create a DynamoDB table object.

        Args:
            table_name: DynamoDB table name

        Returns:
            DynamoDB table object

        """
        return boto3.resource(
            "dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL")
        ).Table(table_name)

    def _load_item_data(self, item: Dict) -> Any:
        """Load item data for a given item metadata dict.

        Args:
            item: Item dict fetched from DynamoDB. If this dict contains an ``s3_key``
                attribute, use that to fetch the data payload. Otherwise, load from the
                ``data`` attribute.

        Returns:
            deserialized item data

        """
        if item.get("s3_key") is not None:
            s3_key = (
                item["s3_key"]["S"]
                if isinstance(item["s3_key"], dict)
                else item["s3_key"]
            )
            response = s3.get_object(Bucket=self.s3_bucket.name, Key=s3_key)
            return json.loads(response["Body"].read())

        data = item["data"]["S"] if isinstance(item["data"], dict) else item["data"]
        return json.loads(data)

    @backoff.on_exception(
        backoff.expo, ClientError, max_tries=5, giveup=_giveup_client_error
    )
    def _get_item(self, table_name: str, partition_key: str, index: int) -> Any:
        """Get data from the table for the given partition key and index.

        Args:
            table_name: DynamoDB table name
            partition_key: Table partition key for the item
            index: Item index. This is only needed when pulling data for an iteration
                in a Map state.

        Returns:
            deserialized JSON value

        Raises:
            :py:exc:`NoItemFound` if no item found

        """
        response = self.table(table_name).get_item(
            Key={"partition_key": partition_key, "sort_key": index}, ConsistentRead=True
        )
        if "Item" in response:
            return self._load_item_data(response["Item"])

        raise NoItemFound(
            f"partition_key={partition_key} sort_key={index} table_name={table_name}"
        )

    def get_item(self, key: str, index: int = 0) -> Any:
        """Get data from the table for the given local key and index.

        This should be called when the key is known to be local to the execution.

        """
        return self._get_item(
            self.default_table_name, self._get_partition_key(key), index
        )

    def get_global_item(
        self, table_name: str, partition_key: str, index: int = 0
    ) -> Any:
        """Get data from the table for the given partition key and index.

        This should be used to get an item that was created by another project.

        """
        return self._get_item(table_name, partition_key, index)

    def get_item_for_map_iteration(self, event: Dict) -> Any:
        """Helper method to get locally-scoped data for a map iteration event object.

        This should be called within a task that serves as a map iterator, and the item
        key is scoped to the current execution.

        """
        return self.get_item(event["items_result_key"], index=event["context_index"])

    def get_global_item_for_map_iteration(self, event: Dict) -> Any:
        """Helper method to get globally-scoped data for a map iteration event object.

        This should be called within a task that serves as a map iterator, and the item
        key is scoped to some other execution.

        """
        return self.get_global_item(
            event["items_result_table_name"],
            event["items_result_partition_key"],
            index=event["context_index"],
        )

    @backoff.on_exception(
        backoff.expo, ClientError, max_tries=5, giveup=_giveup_client_error
    )
    def _get_items(self, table_name: str, partition_key: str) -> List[Dict]:
        """Get all the items from the table for the given partition key.

        Args:
            table_name: DynamoDB table name
            partition_key: Table partition key for the item

        Returns:
            list of items. Each item is the ``data`` attribute. The items will be
                sorted according to the ``index`` attribute

        """
        paginator = dynamodb.get_paginator("query")
        page_iterator = paginator.paginate(
            TableName=table_name,
            ExpressionAttributeNames={"#partition_key": "partition_key"},
            ExpressionAttributeValues={":partition_key": {"S": partition_key}},
            KeyConditionExpression="#partition_key = :partition_key",
        )
        items = [
            self._load_item_data(item)
            for response in page_iterator
            for item in response["Items"]
        ]
        return items

    def get_items(self, key: str) -> List[Dict]:
        """Get all the items from the table for the given local key.

        This should be called when the key is known to be local to the execution.

        """
        return self._get_items(self.default_table_name, self._get_partition_key(key))

    def get_global_items(self, table_name: str, partition_key: str) -> List[Dict]:
        """Get all the items from the table for the given partition key.

        This should be used to get items that were created by another project.

        """
        return self._get_items(table_name, partition_key)

    def _process_item_data(
        self,
        table_name: str,
        partition_key: str,
        data: Any,
        index: int,
        expires_at: int,
    ):
        """Process item data before storing the item.

        If the item's data attribute is larger than a threshold, store it in S3 and
        include an S3 key in the item in DynamoDB.

        Args:
            table_name: DynamoDB table name
            partition_key: Table partition key for the item
            data: Item data. This must be JSON-serializeable.
            index: Item index
            expires_at: Timestamp at which this item will expire

        Returns:
            dict with attributes that can be passed to ``self.table().put_item()``:
            * **partition_key**
            * **sort_key**
            * **expires_at**
            * **data** OR **s3_key** depending on the data size

        """
        serialized_data = json.dumps(data)
        encoded_data = serialized_data.encode("utf-8")
        item = {
            "partition_key": partition_key,
            "sort_key": index,
            "expires_at": expires_at,
        }
        if len(encoded_data) > ITEM_SIZE_THRESHOLD_BYTES:
            s3_key = f"workflow_state_data/{table_name}/{partition_key}/{index}"
            self.s3_bucket.put_object(Key=s3_key, Body=bytes(encoded_data))
            item["s3_key"] = s3_key
        else:
            item["data"] = serialized_data

        return item

    @backoff.on_exception(
        backoff.expo, ClientError, max_tries=5, giveup=_giveup_client_error
    )
    def _put_item(
        self, table_name: str, partition_key: str, data: Any, index: int
    ) -> Dict[str, str]:
        """Put data into the table with the given key.

        Args:
            table_name: DynamoDB table name
            partition_key: Table partition key for the item
            data: Item data. This must be JSON-serializeable.
            index: Item index. This is only needed when putting data for an iteration
                in a Map state.

        Returns:
            dict with keys:
            * **table_name** -- DynamoDB table name
            * **partition_key** -- global key pointing to the collection of state data
              items stored in DynamoDB. This can be used to fetch items in a downstream
              execution.

        """
        self.table(table_name).put_item(
            Item=self._process_item_data(
                table_name, partition_key, data, index, self._get_expires_at()
            )
        )
        return {"table_name": table_name, "partition_key": partition_key}

    def put_item(self, key: str, data: Any, index: int = 0) -> Dict[str, str]:
        """Helper method to put locally-scoped data.

        This should be called when the key is known to be local to the execution.

        Also includes the local key in the result.

        """
        result = self._put_item(
            self.default_table_name, self._get_partition_key(key), data, index
        )
        result["key"] = key
        return result

    def put_global_item(
        self, table_name: str, partition_key: str, data: Any, index: int = 0
    ) -> Dict:
        """Helper method to put globally-scoped data.

        This should be used to update an item that was created by another project.

        """
        return self._put_item(table_name, partition_key, data, index)

    def put_item_for_map_iteration(self, event: Dict, item: Any) -> Dict[str, str]:
        """Helper method to put locally-scoped data for a map iteration event object.

        This should be called within a task that serves as a map iterator, and the item
        key is scoped to the current execution.

        Also includes the local key in the result.

        """
        result = self.put_item(event["items_result_key"], item, event["context_index"])
        result["key"] = event["items_result_key"]
        return result

    def put_global_item_for_map_iteration(
        self, event: Dict, item: Any
    ) -> Dict[str, str]:
        """Helper method to put globally-scoped data for a map iteration event object.

        This should be called within a task that serves as a map iterator, and the item
        key is scoped to some other execution.

        """
        return self.put_global_item(
            event["items_result_table_name"],
            event["items_result_partition_key"],
            item,
            event["context_index"],
        )

    @backoff.on_exception(
        backoff.expo, ClientError, max_tries=5, giveup=_giveup_client_error
    )
    def _put_items(self, table_name: str, partition_key: str, items: List[Any]) -> Dict:
        """Put multiple items into the table with the given partition key.

        Each item will have the ``sort_key`` attribute set to its array index.
        See https://boto3.amazonaws.com/v1/documentation/api/latest/guide/dynamodb.html#batch-writing

        Args:
            table_name: DynamoDB table name
            partition_key: Item partition key. Note that this key is used for all items
                in the batch.
            items: List of items to store. They must be JSON-serializeable.

        Returns:
            dict with keys:
            * **table_name** -- DynamoDB table name
            * **partition_key** -- global key pointing to the collection of state data
              items stored in DynamoDB. This can be used to fetch items in a downstream
              execution.
            * **key** -- local key pointing to the collection of state data items
              stored in DynamoDB. This is passed through from the parameter.
            * **items** -- list of items that will be fanned-out in a Map state. The
              length of the list is the important piece. We'll use the
              ``partition_key``/``key`` and the item index to fetch the actual item
              value from DynamoDB.

        """
        expires_at = self._get_expires_at()
        with self.table(self.default_table_name).batch_writer() as batch:
            for i, data in enumerate(items):
                batch.put_item(
                    Item=self._process_item_data(
                        table_name, partition_key, data, i, expires_at
                    )
                )

        return {
            "table_name": self.default_table_name,
            "partition_key": partition_key,
            "items": [1] * len(items),
        }

    def put_items(self, key: str, items: List[Dict]) -> Dict:
        """Helper method to put multiple locally-scoped items into the table.

        This should be called when the key is known to be local to the execution. Also
        includes the local key in the result. See :py:meth:`._put_items` for argument
        documentation.

        """
        result = self._put_items(
            self.default_table_name, self._get_partition_key(key), items
        )
        result["key"] = key
        return result

    def put_global_items(
        self, table_name: str, partition_key: str, items: List[Dict]
    ) -> Dict:
        """Helper method to put multiple globally-scoped items into the table.

        This should be used to update items that were created by another project. See
        :py:meth:`._put_items` for argument documentation.

        """
        return self._put_items(table_name, partition_key, items)

    def _get_partition_key(self, key: str) -> str:
        """Get a partition key by prefixing the key with the local namespace"""
        return f"{self.namespace}:{key}"

    def _get_expires_at(self) -> int:
        """Get a Unix timestamp for the TTL attribute"""
        timestamp = datetime.datetime.now() + datetime.timedelta(days=self.ttl_days)
        return int(timestamp.timestamp())

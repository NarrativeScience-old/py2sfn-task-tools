from random import choice
from string import ascii_lowercase
import unittest
import uuid

import boto3
import botocore

from py2sfn_task_tools.state_data_client import (
    ITEM_SIZE_THRESHOLD_BYTES,
    StateDataClient,
)

dynamodb = boto3.client("dynamodb")


def create_table(name: str = None):
    """Helper function to create a DynamoDB table for testing.

    This table configuration works with the state data client. All tests can share the
    same table since we'll use a unique execution ID/namespace. The TTL of 1 day will
    ensure the table does not grow indefinitely.
    """
    if name is None:
        name = f"dev-{uuid.uuid4()}"
    table = boto3.resource("dynamodb").Table(name)
    try:
        dynamodb.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "partition_key", "AttributeType": "S"},
                {"AttributeName": "sort_key", "AttributeType": "N"},
            ],
            KeySchema=[
                {"AttributeName": "partition_key", "KeyType": "HASH"},
                {"AttributeName": "sort_key", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            # Table exists
            return table
        raise
    table.wait_until_exists()
    dynamodb.update_time_to_live(
        TableName=name,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
    )
    return table


def create_bucket(name: str = None):
    """Helper function to create an S3 bucket for testing.

    This table configuration works with the state data client. All tests can share the
    same bucket since we'll use a unique execution ID/namespace. The TTL of 1 day will
    ensure the bucket does not grow indefinitely.
    """
    if name is None:
        name = str(uuid.uuid4())
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(name)
    try:
        bucket.create(ACL="private")
        bucket.wait_until_exists()
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise

    boto3.client("s3").put_public_access_block(
        Bucket=bucket.name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    bucket_lifecycle_configuration = bucket.LifecycleConfiguration()
    bucket_lifecycle_configuration.put(
        LifecycleConfiguration={
            "Rules": [
                {
                    "Expiration": {"Days": 1},
                    "ID": "expiration",
                    "Status": "Enabled",
                    "Prefix": "",
                }
            ]
        }
    )

    return bucket


def _build_large_string(size: int = int(1.25 * ITEM_SIZE_THRESHOLD_BYTES)):
    """Helper function to generate a large random string"""
    return "".join([choice(ascii_lowercase) for _ in range(size)])


class StateDataClientFunctionalTests(unittest.TestCase):
    """Functional tests for the state data client"""

    @classmethod
    def setUpClass(cls):
        """Create DynamoDB tables and state data clients"""
        cls.source_table = create_table("py2sfn-task-tools-test-source")
        cls.target_table = create_table("py2sfn-task-tools-test-target")
        cls.s3_bucket = create_bucket("py2sfn-task-tools-test")
        cls.test_execution_id = str(uuid.uuid4())
        cls.source_client = StateDataClient(
            cls.source_table.name,
            cls.test_execution_id,
            ttl_days=1,
            s3_bucket=cls.s3_bucket.name,
        )
        cls.target_client = StateDataClient(
            cls.target_table.name,
            cls.test_execution_id,
            ttl_days=1,
            s3_bucket=cls.s3_bucket.name,
        )

    def test_put_and_get_item(self):
        """Should put and get a local item"""
        data = {"hello": "local"}
        meta = self.source_client.put_item(self.id(), data)
        self.assertEqual(self.source_client.get_item(meta["key"]), data)

    def test_put_and_get_global_item(self):
        """Should put and get a global item"""
        data = {"hello": "global"}
        meta = self.source_client.put_global_item(
            self.source_table.name, self.id(), data, index=42
        )
        self.assertEqual(
            self.target_client.get_global_item(
                meta["table_name"], meta["partition_key"], index=42
            ),
            data,
        )

    def test_put_and_get_item_for_map_iteration(self):
        """Should put and get a local map iteration item"""
        event = {"items_result_key": self.id(), "context_index": 24}
        data = {"hello": "local"}
        self.assertEqual(
            self.source_client.put_item_for_map_iteration(event, data),
            {
                "table_name": self.source_table.name,
                "partition_key": f"{self.source_client.namespace}:{self.id()}",
                "key": self.id(),
            },
        )
        self.assertEqual(self.source_client.get_item_for_map_iteration(event), data)

    def test_put_and_get_global_item_for_map_iteration(self):
        """Should put and get a global map iteration item"""
        event = {
            "items_result_table_name": self.source_table.name,
            "items_result_partition_key": self.id(),
            "context_index": 24,
        }
        data = {"hello": "global"}
        self.assertEqual(
            self.source_client.put_global_item_for_map_iteration(event, data),
            {"table_name": self.source_table.name, "partition_key": self.id()},
        )
        self.assertEqual(
            self.target_client.get_global_item_for_map_iteration(event), data
        )

    def test_put_and_get_items(self):
        """Should put and get a list of local items"""
        items = [{"one": 1}, {"two": 2}]
        meta = self.source_client.put_items(self.id(), items)
        self.assertEqual(
            meta,
            {
                "table_name": self.source_table.name,
                "partition_key": f"{self.source_client.namespace}:{self.id()}",
                "key": self.id(),
                "items": [1, 1],
            },
        )
        self.assertEqual(self.source_client.get_items(meta["key"]), items)

    def test_put_and_get_global_items(self):
        """Should put and get a list of global items"""
        items = [{"one": 1}, {"two": 2}]
        meta = self.source_client.put_global_items(
            self.source_table.name, f"{self.source_client.namespace}:{self.id()}", items
        )
        self.assertEqual(
            meta,
            {
                "table_name": self.source_table.name,
                "partition_key": f"{self.source_client.namespace}:{self.id()}",
                "items": [1, 1],
            },
        )
        self.assertEqual(
            self.target_client.get_global_items(
                meta["table_name"], meta["partition_key"]
            ),
            items,
        )

    def test_put_and_get_item__large(self):
        """Should put and get a large item"""
        data = {"big": _build_large_string()}
        meta = self.source_client.put_item(self.id(), data)
        self.assertEqual(self.source_client.get_item(meta["key"]), data)

    def test_put_and_get_items__large(self):
        """Should put and get a list of large items intermixed with small ones"""
        items = [
            {"one": _build_large_string()},
            {"two": "lil"},
            {"three": _build_large_string()},
        ]
        meta = self.source_client.put_items(self.id(), items)
        self.assertEqual(
            meta,
            {
                "table_name": self.source_table.name,
                "partition_key": f"{self.source_client.namespace}:{self.id()}",
                "key": self.id(),
                "items": [1, 1, 1],
            },
        )
        self.assertEqual(self.source_client.get_items(meta["key"]), items)


if __name__ == "__main__":
    unittest.main()

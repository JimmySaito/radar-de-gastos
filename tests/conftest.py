from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "UsageHubTest")
os.environ.setdefault("POWERTOOLS_DEV", "false")

from usage_hub.domain.models import Provider, UsageRecord

RecordFactory = Callable[..., UsageRecord]


@pytest.fixture
def make_record() -> RecordFactory:
    def factory(**overrides: Any) -> UsageRecord:
        values: dict[str, Any] = {
            "tenant_id": "acme",
            "provider": Provider.ANTHROPIC,
            "service": "claude-opus-5-5",
            "principal": "user:alice",
            "usage_date": date(2026, 11, 10),
            "input_tokens": 1000,
            "output_tokens": 500,
            "requests": 10,
            "cost": Decimal("1.25"),
        }
        values.update(overrides)
        return UsageRecord(**values)

    return factory


@pytest.fixture
def dynamodb_table() -> Iterator[tuple[Any, str]]:
    from moto import mock_aws

    with mock_aws():
        import boto3

        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="usagehub-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
        )
        yield client, "usagehub-test"

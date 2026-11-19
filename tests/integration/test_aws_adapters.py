from __future__ import annotations

import json
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from radar_gastos.adapters.aws import SecretsManagerProvider, SnsAlertPublisher, SqsWorkQueue

pytestmark = pytest.mark.integration


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


def test_secrets_are_cached(aws: None) -> None:
    client = boto3.client("secretsmanager")
    client.create_secret(
        Name="radar-gastos/acme/openai", SecretString=json.dumps({"admin_api_key": "v1"})
    )
    provider = SecretsManagerProvider(client, ttl_seconds=60)
    assert provider.get_secret("radar-gastos/acme/openai") == {"admin_api_key": "v1"}
    client.put_secret_value(
        SecretId="radar-gastos/acme/openai", SecretString=json.dumps({"admin_api_key": "v2"})
    )
    assert provider.get_secret("radar-gastos/acme/openai")["admin_api_key"] == "v1"  # cacheado
    assert (
        SecretsManagerProvider(client, ttl_seconds=0).get_secret("radar-gastos/acme/openai")[
            "admin_api_key"
        ]
        == "v2"
    )


def test_sqs_batches_in_groups_of_ten(aws: None) -> None:
    client = boto3.client("sqs")
    url = client.create_queue(QueueName="work")["QueueUrl"]
    assert SqsWorkQueue(client, url).send_batch([{"i": i} for i in range(23)]) == 23
    attrs = client.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
    )
    assert attrs["Attributes"]["ApproximateNumberOfMessages"] == "23"


def test_sns_publish(aws: None) -> None:
    client = boto3.client("sns")
    arn = client.create_topic(Name="alerts")["TopicArn"]
    SnsAlertPublisher(client, arn).publish("x" * 200, {"a": 1})  # el subject se trunca a 100


def test_secret_store_create_rotate_delete(aws: None) -> None:
    client = boto3.client("secretsmanager")
    store = SecretsManagerProvider(client, ttl_seconds=0)
    arn = store.put_secret("acme", "openai-1", {"admin_api_key": "v1"})
    assert store.get_secret(arn) == {"admin_api_key": "v1"}
    arn2 = store.put_secret("acme", "openai-1", {"admin_api_key": "v2"})  # ya existe → rota
    assert store.get_secret(arn2)["admin_api_key"] == "v2"
    store.delete_secret(arn2)
    described = client.describe_secret(SecretId=arn2)
    assert "DeletedDate" in described  # recuperable durante la ventana de 7 días

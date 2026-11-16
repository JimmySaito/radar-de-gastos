"""Adaptadores de AWS para secretos, colas y alertas."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

SQS_MAX_BATCH = 10


class SecretsManagerProvider:
    """Lee secretos JSON con caché TTL en memoria (reduce latencia y costo por invocación)."""

    def __init__(self, client: Any, ttl_seconds: float = 300.0) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Mapping[str, str]]] = {}

    def get_secret(self, secret_ref: str) -> Mapping[str, str]:
        cached = self._cache.get(secret_ref)
        now = time.monotonic()
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        value: Mapping[str, str] = json.loads(
            self._client.get_secret_value(SecretId=secret_ref)["SecretString"]
        )
        self._cache[secret_ref] = (now, value)
        return value


class SqsWorkQueue:
    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._url = queue_url

    def send_batch(self, messages: Sequence[Mapping[str, object]]) -> int:
        sent = 0
        for offset in range(0, len(messages), SQS_MAX_BATCH):
            chunk = messages[offset : offset + SQS_MAX_BATCH]
            response = self._client.send_message_batch(
                QueueUrl=self._url,
                Entries=[
                    {"Id": str(i), "MessageBody": json.dumps(m, default=str)}
                    for i, m in enumerate(chunk)
                ],
            )
            if failed := response.get("Failed"):
                raise RuntimeError(
                    f"SQS rechazó {len(failed)} mensajes: {failed[0].get('Message')}"
                )
            sent += len(chunk)
        return sent


class SnsAlertPublisher:
    def __init__(self, client: Any, topic_arn: str) -> None:
        self._client = client
        self._topic = topic_arn

    def publish(self, subject: str, message: Mapping[str, object]) -> None:
        self._client.publish(
            TopicArn=self._topic, Subject=subject[:100], Message=json.dumps(message, default=str)
        )

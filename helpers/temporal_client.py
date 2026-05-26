"""Shared Temporal client factory for activities that need to talk to Temporal directly."""

from __future__ import annotations

import os

from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter


async def connect() -> Client:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    tls: TLSConfig | bool = False
    cert_path = os.environ.get("TEMPORAL_TLS_CERT")
    key_path = os.environ.get("TEMPORAL_TLS_KEY")
    if cert_path and key_path:
        tls = TLSConfig(
            client_cert=open(cert_path, "rb").read(),
            client_private_key=open(key_path, "rb").read(),
        )
    return await Client.connect(
        address, namespace=namespace, tls=tls, data_converter=pydantic_data_converter
    )

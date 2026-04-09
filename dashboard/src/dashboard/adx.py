"""Shared ADX (Kusto) client factory."""

from azure.identity import AzureCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder


def get_client(cluster_uri: str) -> KustoClient:
    credential = AzureCliCredential()
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(cluster_uri, credential)
    return KustoClient(kcsb)

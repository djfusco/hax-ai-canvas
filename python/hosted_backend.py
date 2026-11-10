"""Hosted persistence: workspaces, saved AI keys and accounts in Postgres."""

from hosted_config import settings


class HostedBackend:
    def __init__(self, config=None):
        self.config = config or settings()
        from hosted_data import PostgresMentorStore, PostgresCredentialVault
        self.store = PostgresMentorStore(self.config.database_url)
        self.vault = PostgresCredentialVault(self.config.database_url, self.config.encryption_key)
        if self.config.auth_mode == "neon":
            from hosted_accounts import PostgresAccounts
            self.accounts = PostgresAccounts(self.config.database_url)

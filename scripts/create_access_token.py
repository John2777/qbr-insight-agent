from __future__ import annotations

import argparse

from packages.qbr_core.auth import create_hs256_token
from packages.qbr_core.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a short-lived JWT for the QBR demo deployment")
    parser.add_argument("--user", default="user_demo")
    parser.add_argument("--workspace", default="ws_demo")
    parser.add_argument("--role", choices=["viewer", "editor", "reviewer", "admin"], default="admin")
    parser.add_argument("--ttl", type=int, default=86_400, help="Lifetime in seconds")
    arguments = parser.parse_args()
    settings = Settings.from_env()
    if settings.auth_mode != "jwt":
        raise SystemExit("Set AUTH_MODE=jwt and JWT_SECRET in .env before creating a token")
    print(
        create_hs256_token(
            settings,
            user_id=arguments.user,
            workspace_id=arguments.workspace,
            roles=[arguments.role],
            ttl_seconds=arguments.ttl,
        )
    )


if __name__ == "__main__":
    main()

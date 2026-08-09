from __future__ import annotations

import getpass

from packages.qbr_core.auth import create_password_hash


def main() -> None:
    first = getpass.getpass("Demo password (12+ characters): ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords do not match")
    print(create_password_hash(first))


if __name__ == "__main__":
    main()

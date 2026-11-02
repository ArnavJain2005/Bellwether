"""Re-export of sample database helpers from ``dashboard.tests.sample_db``."""

from __future__ import annotations

from dashboard.tests.sample_db import CONTRACT_DDL, create_sample_state, main

__all__ = ["CONTRACT_DDL", "create_sample_state", "main"]

if __name__ == "__main__":
    main()

"""Shared account-scope resolution helpers for API routers."""

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import is_test_mode
from app.models import Account


# Accounts we want the system to completely ignore
EXCLUDED_ACCOUNT_IDS = {
    "7f0a6253-777b-4b8f-abfb-48dc2d66de68",
}


def resolve_account_ids(
    db: Session,
    account_id: Optional[str],
    *,
    no_accounts_message: str = "No accounts discovered.",
) -> List[str]:
    """Resolve account selector into one or more sub-account IDs.

    - None -> first visible sub-account (backward compatibility)
    - "<uuid>" -> specific sub-account
    - "all" -> all visible sub-accounts
    - "all:<credential_name>" -> all visible sub-accounts under one credential
    """

    test_mode = is_test_mode()

    # --------------------------------------------------
    # ALL ACCOUNTS
    # --------------------------------------------------
    if account_id == "all":
        query = db.query(Account)

        if test_mode:
            query = query.filter(Account.credential_name == "__TEST__")
        else:
            query = query.filter(Account.credential_name != "__TEST__")

        query = query.filter(Account.status == "ACTIVE")
        query = query.filter(~Account.id.in_(EXCLUDED_ACCOUNT_IDS))

        accts = query.all()

        if not accts:
            raise HTTPException(404, no_accounts_message)

        return [a.id for a in accts]

    # --------------------------------------------------
    # ALL ACCOUNTS UNDER ONE CREDENTIAL
    # --------------------------------------------------
    if account_id and account_id.startswith("all:"):
        cred_name = account_id[4:]

        if test_mode and cred_name != "__TEST__":
            raise HTTPException(404, "Only __TEST__ accounts are available in test mode")

        if not test_mode and cred_name == "__TEST__":
            raise HTTPException(404, "Test mode is not enabled")

        accts = (
            db.query(Account)
            .filter_by(credential_name=cred_name)
            .filter(Account.status == "ACTIVE")
            .filter(~Account.id.in_(EXCLUDED_ACCOUNT_IDS))
            .all()
        )

        if not accts:
            raise HTTPException(404, f"No sub-accounts found for credential '{cred_name}'")

        return [a.id for a in accts]

    # --------------------------------------------------
    # SPECIFIC ACCOUNT
    # --------------------------------------------------
    if account_id:
        acct = db.query(Account).filter_by(id=account_id).first()

        if not acct:
            raise HTTPException(404, f"Account {account_id} not found")

        if acct.id in EXCLUDED_ACCOUNT_IDS:
            raise HTTPException(404, f"Account {account_id} not available")

        if test_mode and acct.credential_name != "__TEST__":
            raise HTTPException(404, "Only __TEST__ accounts are available in test mode")

        if not test_mode and acct.credential_name == "__TEST__":
            raise HTTPException(404, "Test mode is not enabled")

        return [account_id]

    # --------------------------------------------------
    # DEFAULT ACCOUNT (first visible)
    # --------------------------------------------------
    query = db.query(Account)

    if test_mode:
        query = query.filter(Account.credential_name == "__TEST__")
    else:
        query = query.filter(Account.credential_name != "__TEST__")

    query = query.filter(Account.status == "ACTIVE")
    query = query.filter(~Account.id.in_(EXCLUDED_ACCOUNT_IDS))

    first = query.first()

    if not first:
        raise HTTPException(404, no_accounts_message)

    return [first.id]
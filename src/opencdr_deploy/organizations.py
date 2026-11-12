"""Optional bounded discovery on an explicitly selected customer workstation profile."""

import json
import re

import boto3

from .fleet_api import FleetManagerError, aws_client, verify_account


def configure(commands):
    parser = commands.add_parser(
        "organizations",
        help="read-only account discovery; never registers accounts or changes Fleet",
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument(
        "--account-id", required=True, help="expected identity of the Organizations reader"
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--after", help="AWS-issued continuation token")


def run(args):
    if not re.fullmatch(r"[0-9]{12}", args.account_id) or not 1 <= args.limit <= 20:
        raise FleetManagerError("Use an exact expected account and a page limit from 1 to 20")
    if args.after is not None and (
        not isinstance(args.after, str) or not 1 <= len(args.after) <= 4096
    ):
        raise FleetManagerError("Invalid Organizations continuation token")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    verify_account(session, args.account_id)
    page = aws_client(session, "organizations").list_accounts(
        MaxResults=args.limit, **({"NextToken": args.after} if args.after else {})
    )
    rows = []
    for account in page.get("Accounts", []):
        if not re.fullmatch(r"[0-9]{12}", str(account.get("Id", ""))):
            raise FleetManagerError("Invalid discovered account identity")
        rows.append(
            {
                "account_id": account["Id"],
                "name": account.get("Name", ""),
                "state": account.get("State", account.get("Status", "unknown")),
            }
        )
    print(
        json.dumps(
            {
                "reader_account_id": args.account_id,
                "accounts": rows,
                "next_after": page.get("NextToken"),
                "discovery_only": True,
                "registered": False,
                "snapshot": False,
            },
            indent=2,
        )
    )
    return 0

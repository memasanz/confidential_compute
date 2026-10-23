"""Delete worker container groups older than a maximum age, using ARM metadata only.

Runs independently of the pool controller and gateway: it needs no session secrets,
no worker connection and no session content. It only reads group tags and start
times and deletes groups. Schedule it (for example as a Container Apps job) with an
identity that has Contributor on the worker resource group and nothing else.

    python orphan_cleanup.py --subscription SUB --resource-group RG --pool POOL \
        --max-age-minutes 120 [--dry-run]
"""

import argparse
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient


def started_at(group):
    """Earliest container start time, or None while the group has not started."""
    starts = [c.instance_view.current_state.start_time for c in group.containers
              if c.instance_view and c.instance_view.current_state
              and c.instance_view.current_state.start_time]
    return min(starts) if starts else None


def expired_groups(groups, pool, now, max_age):
    """Groups of this pool whose containers started more than max_age ago."""
    expired = []
    for group in groups:
        if (group.tags or {}).get("pool-control") != pool:
            continue
        start = started_at(group)
        if start is not None and now - start > max_age:
            expired.append(group)
    return expired


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--max-age-minutes", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = ContainerInstanceManagementClient(DefaultAzureCredential(), args.subscription)
    groups = list(client.container_groups.list_by_resource_group(args.resource_group))
    for group in expired_groups(groups, args.pool, datetime.now(timezone.utc),
                                timedelta(minutes=args.max_age_minutes)):
        print(f"{'would delete' if args.dry_run else 'deleting'} {group.name}")
        if not args.dry_run:
            client.container_groups.begin_delete(args.resource_group, group.name)


if __name__ == "__main__":
    main()

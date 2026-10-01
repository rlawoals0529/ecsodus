"""ecsodus command line (PLAN §2).

    ecsodus inventory --app APP [--env ENV ...] [--keep-on-copilot NAME ...] -o inventory.json
    ecsodus report    inventory.json [-o REPORT.md] [--html]
    ecsodus generate  inventory.json --out DIR [--patch-bucket B] [--metadata-fallback]
    ecsodus check     PLAN.json --manifest M --phase import|steady
    ecsodus check     --state state.txt --manifest M
    ecsodus check     --changeset CS.json ... --manifest M --stack S [--allow-metadata-key]
    ecsodus check     --template-diff CURRENT PATCHED
    ecsodus verify-retain --app APP [--stack S ...]

Exit codes: 0 ok, 1 check failed, 2 usage/input error, 3 change set empty (see RUNBOOK step 3).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ecsodus import __version__
from ecsodus.model import Inventory, write_sensitive

MAX_INVENTORY_AGE_S = 24 * 3600


def _err(msg: str) -> int:
    print(f"ecsodus: {msg}", file=sys.stderr)
    return 2


def _load_inventory(path: str, allow_stale: bool = False) -> Inventory:
    inv = Inventory.load(path)
    if not allow_stale and inv.age_seconds() > MAX_INVENTORY_AGE_S:
        raise SystemExit(
            _err(
                f"inventory {path} is older than 24h (captured {inv.captured_at}); re-run "
                "`ecsodus inventory` (or pass --allow-stale for offline review only)"
            )
        )
    return inv


# -- inventory -----------------------------------------------------------------------------
def cmd_inventory(a: argparse.Namespace) -> int:
    import boto3

    from ecsodus.sources import copilot
    from ecsodus.sources.aws import Clients

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    inv = copilot.inventory(Clients(session), a.app, a.env or None, a.keep_on_copilot or ())
    if a.profile:
        inv.selection["profile"] = a.profile
    if not inv.stacks:
        return _err(f"no Copilot stacks found for app {a.app!r} in {inv.account}/{inv.region}")
    inv.save(a.output)
    print(
        f"wrote {a.output} (mode 0600): {len(inv.stacks)} stacks, "
        f"{sum(len(s.resources) for s in inv.stacks.values())} resources, "
        f"{len(inv.unavailable)} unavailable"
    )
    return 0


# -- report --------------------------------------------------------------------------------
def cmd_report(a: argparse.Namespace) -> int:
    from ecsodus.emit import report
    from ecsodus.mappers.fates import build_plan

    inv = _load_inventory(a.inventory, allow_stale=True)
    plan = build_plan(inv)
    text = report.render_html(plan) if a.html else report.render(plan)
    output = a.output or ("REPORT.html" if a.html else "REPORT.md")
    if output == "-":
        sys.stdout.write(text)
    else:
        Path(output).write_text(text)
        print(f"wrote {output}")
    return 0


# -- generate ------------------------------------------------------------------------------
def cmd_generate(a: argparse.Namespace) -> int:
    from ecsodus.emit import report, runbook, terraform
    from ecsodus.emit.patches import build_patches, default_patch_bucket
    from ecsodus.mappers.fates import build_plan

    inv = _load_inventory(a.inventory, allow_stale=a.allow_stale)
    plan = build_plan(inv)
    out = Path(a.out)
    bucket = a.patch_bucket or default_patch_bucket(inv)
    if not bucket:
        return _err("no Copilot artifact bucket found; pass --patch-bucket")
    handoff = {n for n, sp in plan.stacks.items() if sp.handoff}
    patches = build_patches(inv, bucket, metadata_fallback=a.metadata_fallback, only=handoff)
    (out / "retain-patches").mkdir(parents=True, exist_ok=True)
    for name, p in patches.stacks.items():
        write_sensitive(out / "retain-patches" / f"{name}.yml", p.result.text)
    for name, r in patches.stackset.items():
        write_sensitive(out / "retain-patches" / f"stackset-{name}.yml", r.text)
    (out / "REPORT.md").write_text(report.render(plan))
    (out / "RUNBOOK.md").write_text(
        runbook.render(
            plan,
            patches,
            include_teardown=True,
            out_dir=str(out),
            inventory_path=a.inventory,
        )
    )
    if plan.closure_errors:
        print("closure check FAILED; Terraform not generated. See REPORT.md:", file=sys.stderr)
        for e in plan.closure_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    written = terraform.write_project(plan, patches, out)
    n_imports = len(plan.imports())
    print(
        f"wrote {len(written)} Terraform files ({n_imports} imports), "
        f"{len(patches.stacks)} retain patches, REPORT.md, RUNBOOK.md to {out}/"
    )
    kept = [n for n, s in plan.stacks.items() if not s.handoff]
    if kept:
        print(f"{len(kept)} stack(s) kept on Copilot; see REPORT.md")
    return 0


# -- check ---------------------------------------------------------------------------------
def _manifest(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def cmd_check(a: argparse.Namespace) -> int:
    from ecsodus.check import changeset
    from ecsodus.check import plan as plan_check

    if a.template_diff:
        from ecsodus.emit.retain_patch import verify_patch

        current, patched = (Path(p).read_text() for p in a.template_diff)
        problems = verify_patch(current, patched, metadata_fallback=a.allow_metadata_key)
        for p in problems:
            print(f"FAIL {p}")
        print("template diff: " + ("only retain-patch edits" if not problems else "REJECTED"))
        return 1 if problems else 0

    if not a.manifest:
        return _err("--manifest is required")
    m = _manifest(a.manifest)
    expected = {i["address"]: i["id"] for i in m["imports"]}
    if a.phase == "import" and not a.allow_stale:
        from datetime import UTC, datetime

        age = datetime.now(UTC) - datetime.fromisoformat(m["inventory_captured_at"])
        if age.total_seconds() > MAX_INVENTORY_AGE_S:
            return _err(
                "the manifest was generated from an inventory older than 24h; re-run "
                "`ecsodus inventory` and `ecsodus generate` before importing"
            )

    if a.changeset:
        docs = [json.loads(Path(p).read_text()) for p in a.changeset]
        nested: dict[str, str] = {}
        patches = m["retain_patches"]
        for s in [a.stack] if a.stack else list(patches):
            for lid, info in ((patches.get(s) or {}).get("nested") or {}).items():
                nested[lid] = patches[info["stack"]]["url"]
        cs_res = changeset.check_change_sets(
            docs,
            patched_nested=nested,
            allow_metadata_key=a.allow_metadata_key,
            allow_nested_dynamic=a.allow_nested_dynamic,
        )
        for e in cs_res.errors:
            print(f"FAIL {e}")
        if cs_res.verdict == changeset.EMPTY:
            print(
                "EMPTY change set: run `ecsodus verify-retain`; skip the stack if it passes, "
                "otherwise regenerate with --metadata-fallback (RUNBOOK step 3)"
            )
            return 3
        print(
            f"change sets: {cs_res.change_sets}, accepted changes: {cs_res.accepted}, "
            f"verdict: {cs_res.verdict.upper()}"
        )
        return 0 if cs_res.ok else 1

    if a.state:
        res = plan_check.check_state(Path(a.state).read_text().splitlines(), expected)
    else:
        if not a.plan or not a.phase:
            return _err("give a plan JSON and --phase, or --state, or --changeset")
        doc = json.loads(Path(a.plan).read_text())
        task_defs = {i["address"] for i in m["imports"] if i["type"] == "AWS::ECS::TaskDefinition"}
        for f in a.forgotten or ():
            if f not in task_defs:
                return _err(f"--forgotten {f} is not an imported task definition in the manifest")
        res = plan_check.check_plan(doc, expected, a.phase, a.forgotten or ())
    for e in res.errors:
        print(f"FAIL {e}")
    for w in res.warnings:
        print(f"warn {w}")
    print(f"{'PASS' if res.ok else 'FAIL'} {json.dumps(res.summary, sort_keys=True)}")
    return 0 if res.ok else 1


# -- verify-retain -------------------------------------------------------------------------
def cmd_verify_retain(a: argparse.Namespace) -> int:
    """Read-only: every resource of every stack (nested included) carries both Retain policies.

    With ``--manifest`` the expected stack set is the manifest's hand-off set, every expected
    stack must be found, and each patched stack's deployed template must hash to the patched
    template the manifest recorded. Finding no stack at all is a failure, never a pass.
    """
    import boto3

    from ecsodus.emit.retain_patch import missing_retain, sha256
    from ecsodus.sources.aws import Clients, paginate

    clients = Clients(boto3.Session(profile_name=a.profile, region_name=a.region))
    cfn = clients("cloudformation")
    manifest = _manifest(a.manifest) if a.manifest else None
    names = list(a.stack or [])
    if not names and manifest:
        names = [
            n
            for n in manifest["handoff_stacks"]
            if not (manifest["retain_patches"].get(n) or {}).get("parent")
        ]
    if not names:
        for s in paginate(cfn, "list_stacks", "StackSummaries"):
            if s.get("StackStatus") == "DELETE_COMPLETE":
                continue
            d = cfn.describe_stacks(StackName=s["StackId"])["Stacks"][0]
            tags = {t["Key"]: t["Value"] for t in d.get("Tags") or []}
            if tags.get("copilot-application") == a.app and not d.get("ParentId"):
                names.append(d["StackName"])
    if not names:
        print(f"FAIL no stacks found for app {a.app!r}; wrong app, account or region?")
        return 1
    failed = False
    seen: set[str] = set()
    checked: set[str] = set()

    def check(stack_ref: str, label: str) -> None:
        nonlocal failed
        if stack_ref in seen:
            return
        seen.add(stack_ref)
        try:
            desc = cfn.describe_stacks(StackName=stack_ref)["Stacks"][0]
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"FAIL {label}: cannot describe stack: {exc}")
            return
        name = desc["StackName"]
        checked.add(name)
        body = cfn.get_template(StackName=stack_ref, TemplateStage="Original")["TemplateBody"]
        if not isinstance(body, str):
            body = json.dumps(body)
        missing = missing_retain(body)
        if missing:
            failed = True
            print(
                f"FAIL {label}: {len(missing)} resource(s) without Retain: "
                + ", ".join(missing[:10])
                + (" ..." if len(missing) > 10 else "")
            )
        else:
            print(f"ok   {label}")
        if manifest:
            want = (manifest["retain_patches"].get(name) or {}).get("sha256")
            if want and sha256(body) != want:
                failed = True
                print(
                    f"FAIL {label}: deployed template does not match the patched template "
                    "recorded in the manifest"
                )
        for r in paginate(
            cfn, "list_stack_resources", "StackResourceSummaries", StackName=stack_ref
        ):
            if r["ResourceType"] == "AWS::CloudFormation::Stack" and r.get("PhysicalResourceId"):
                check(r["PhysicalResourceId"], f"{label}/{r['LogicalResourceId']}")

    for n in names:
        check(n, n)
    if manifest and not a.stack:
        for missing_stack in sorted(set(manifest["handoff_stacks"]) - checked):
            failed = True
            print(f"FAIL {missing_stack}: expected by the manifest but not found")
    return 1 if failed else 0


def cmd_verify_fresh(a: argparse.Namespace) -> int:
    """Read-only: the account, region and stacks are exactly those the manifest was made from."""
    import boto3

    from ecsodus.sources.aws import Clients

    m = _manifest(a.manifest)
    clients = Clients(boto3.Session(profile_name=a.profile, region_name=a.region))
    failed = False
    account = clients("sts").get_caller_identity()["Account"]
    if account != m["account"] or clients.region != m["region"]:
        print(
            f"FAIL caller is {account}/{clients.region}, manifest is {m['account']}/{m['region']}"
        )
        return 1
    cfn = clients("cloudformation")
    for name, recorded in sorted(m["stack_last_updated"].items()):
        try:
            d = cfn.describe_stacks(StackName=name)["Stacks"][0]
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"FAIL {name}: {exc}")
            continue
        status = str(d.get("StackStatus", ""))
        if not status.endswith("_COMPLETE") or "ROLLBACK" in status or status.startswith("DELETE"):
            failed = True
            print(f"FAIL {name}: status {d.get('StackStatus')} (must be settled *_COMPLETE)")
            continue
        last = d.get("LastUpdatedTime") or d.get("CreationTime")
        now = last.isoformat() if hasattr(last, "isoformat") else str(last)
        if now != recorded:
            failed = True
            print(
                f"FAIL {name}: changed since the inventory ({recorded} -> {now}); "
                "re-run inventory and generate"
            )
        else:
            print(f"ok   {name}")
    return 1 if failed else 0


# -- parser --------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ecsodus", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"ecsodus {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def aws_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--profile")
        sp.add_argument("--region")

    s = sub.add_parser("inventory", help="read a Copilot app from AWS (read-only)")
    s.add_argument("--app", required=True)
    s.add_argument("--env", action="append", help="limit to this environment (repeatable)")
    s.add_argument(
        "--keep-on-copilot",
        action="append",
        metavar="[ENV/]WORKLOAD",
        help="workload that stays on Copilot (partial migration)",
    )
    s.add_argument("-o", "--output", default="inventory.json")
    aws_opts(s)
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser("report", help="write the readiness report")
    s.add_argument("inventory")
    s.add_argument("-o", "--output")
    s.add_argument("--html", action="store_true", help="write a standalone HTML report")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("generate", help="write Terraform, retain patches, report and runbook")
    s.add_argument("inventory")
    s.add_argument("--out", required=True)
    s.add_argument(
        "--patch-bucket",
        help="S3 bucket for patched templates (default: the Copilot artifact bucket)",
    )
    s.add_argument(
        "--metadata-fallback",
        action="store_true",
        help="add an ecsodus:retain Metadata key per resource (RUNBOOK step 3)",
    )
    # Deprecated no-op: the teardown gate was removed after the AWS e2e run (ADR-0012).
    s.add_argument(
        "--i-understand-teardown-is-unverified", action="store_true", help=argparse.SUPPRESS
    )
    s.add_argument(
        "--allow-stale",
        action="store_true",
        help="accept an inventory older than 24h (offline review only)",
    )
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("check", help="gate a Terraform plan, state, or change set")
    s.add_argument("plan", nargs="?", help="terraform show -json output")
    s.add_argument("--manifest", help="ecsodus-manifest.json from generate")
    s.add_argument("--phase", choices=["import", "steady"])
    s.add_argument("--state", help="terraform state list output")
    s.add_argument("--changeset", nargs="+", help="describe-change-set JSON files")
    s.add_argument("--stack", help="stack the change sets belong to")
    s.add_argument("--allow-metadata-key", action="store_true")
    s.add_argument(
        "--allow-nested-dynamic",
        action="store_true",
        help="accept Dynamic entries caused by patched nested stacks (PLAN §13.3)",
    )
    s.add_argument("--template-diff", nargs=2, metavar=("CURRENT", "PATCHED"))
    s.add_argument("--allow-stale", action="store_true", help="skip the 24h manifest check")
    s.add_argument(
        "--forgotten",
        action="append",
        metavar="ADDRESS",
        help="address handed off with a removed{destroy=false} block (steady phase)",
    )
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("verify-retain", help="confirm every stack resource is Retain (read-only)")
    s.add_argument("--app", required=True)
    s.add_argument("--stack", action="append")
    s.add_argument("--manifest", help="require the manifest's stacks and patched template hashes")
    aws_opts(s)
    s.set_defaults(fn=cmd_verify_retain)

    s = sub.add_parser("verify-fresh", help="confirm account, region and stacks match the manifest")
    s.add_argument("--manifest", required=True)
    aws_opts(s)
    s.set_defaults(fn=cmd_verify_fresh)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (ValueError, FileNotFoundError) as exc:
        return _err(str(exc))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

_ = write_sensitive

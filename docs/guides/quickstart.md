# Quickstart

> ecsodus v0.1.0rc1. Read [how it works](how-it-works.md) before running anything against
> production.

## 0. Prerequisites

- Python ≥ 3.11 and `uv` (or `pipx`). Terraform ≥ 1.10.
- AWS credentials for the account and region of the Copilot app, with read access. ecsodus only
  calls `Describe*`/`List*`/`Get*`. The runbook's mutating steps are yours to run, with your own
  credentials.
- An S3 bucket for encrypted, locked Terraform state.

```bash
uv tool install ecsodus        # after the public release; until then: uv run from a checkout
```

## 1. Inventory (read-only)

```bash
ecsodus inventory --app myapp --region us-west-2 -o inventory.json
```

This writes `inventory.json` with mode 0600, because task-definition environment values are
plaintext. It is never committed (see `.gitignore`).

For a **partial migration**, keep some workloads on Copilot:

```bash
ecsodus inventory --app myapp --keep-on-copilot test/worker -o inventory.json
```

Shared stacks those workloads need (their environment, the app stack) are then kept too.

## 2. Read the report

```bash
ecsodus report inventory.json -o REPORT.md
```

For a standalone copy you can open in a browser or send to someone who does not use GitHub:

```bash
ecsodus report inventory.json --html -o REPORT.html
```

The report covers:
- the verdict
- each workload's status
- per stack: hand off or kept, and why
- for each stack, what deleting it *without* the retain patch would destroy
- blocked resources
- manual-cleanup items
- Express Mode fit
- the keep-CloudFormation baseline

## 3. Generate

```bash
ecsodus generate inventory.json --out infra/
```

This writes to `infra/`:
- Terraform: one file per handed-off stack, with `import` blocks
- `retain-patches/`
- `ecsodus-manifest.json`
- `REPORT.md`
- `RUNBOOK.md`

The retain patches are uploaded to Copilot's artifact bucket by default; override with
`--patch-bucket`.

## 4. Follow RUNBOOK.md

1. Freeze
2. Protect
3. Retain patches
4. Import
5. Teardown
6. Verify
7. The "never" list

Each mutating command is preceded by the ecsodus check that gates it. Stop at the first failure.

Every step, including step 5 (teardown), is verified on real AWS (2026-09-30). The runbook
banner lists what that run did not cover: custom domains, Aurora, NAT, and partial migrations.

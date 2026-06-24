# AWS ECR FinOps Artifact Optimizer

A lightweight Python utility that reclaims AWS Elastic Container Registry (ECR) storage by pruning untagged and aged container images across your repositories — safely, predictably, and on your terms.

---

## Problem Statement

Container registries grow silently. Every CI pipeline run, every rebuilt layer, and every abandoned feature branch pushes new image manifests into ECR — and almost nothing ever removes them.

This creates two compounding costs:

- **Direct storage spend.** ECR bills per GB-month. Untagged images left behind by tag re-pushes, plus stale release artifacts no environment references anymore, accumulate into a line item that only ever trends upward.
- **Security and compliance surface.** Old images often bundle outdated base layers and unpatched CVEs. Keeping them around extends your vulnerability footprint and muddies audits, since a registry full of ambiguous artifacts makes it harder to prove what is actually deployable.

Manual cleanup does not scale beyond a handful of repositories, and ECR lifecycle policies can be coarse and hard to reason about. This tool gives you an explicit, auditable, config-driven cleanup process you can run on demand or schedule in CI.

---

## Architecture & Features

The optimizer is a single, dependency-light script (`cleaner.py`) that walks every repository in the account, identifies stale images, and removes them according to your policy.

- **Dry-run by default.** Every run reports exactly which images *would* be deleted before anything is destroyed. Real deletions require an explicit opt-in, making accidental data loss difficult.
- **YAML configuration.** Retention window, dry-run posture, and repository exclusions live in a version-controllable `config.yaml`, keeping policy separate from code.
- **CLI overrides.** Command-line flags override config values at invocation time, so the same script serves local previews, scheduled jobs, and one-off forced cleanups.
- **Multi-repository support.** Automatically discovers and processes every repository in the account/region via paginated ECR APIs — no per-repo wiring required.
- **Repository exclusions.** An allowlist of protected repositories is skipped entirely, shielding critical or shared image stores.
- **Staleness logic.** An image is targeted if it is **untagged** *or* older than the configured retention window (based on `imagePushedAt`).
- **Pagination-aware and batch-safe.** Handles large repositories transparently and deletes in API-compliant batches of up to 100 images per call, logging both successes and failures.

### How it works

```
load config (+ CLI overrides)
        │
        ▼
describe_repositories  ──►  for each repository
        │                        │
        │                  excluded?  ──► skip
        │                        │
        │                        ▼
        │                  get_stale_images()
        │                  (untagged OR age > retention)
        │                        │
        │                        ▼
        │                  clean_repository()
        │                  dry-run: report only
        │                  force:   batch_delete_image()
        ▼
summary: total images processed
```

---

## Prerequisites

- **Python 3.9+**
- **An AWS account** with one or more ECR repositories
- **AWS credentials** with permissions for:
  - `ecr:DescribeRepositories`
  - `ecr:DescribeImages`
  - `ecr:BatchDeleteImage`

---

## Quick Start Guide

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure AWS credentials

Use any standard credential source supported by `boto3` — a named profile, environment variables, or an instance/role.

```bash
# Option A: named profile
export AWS_PROFILE=my-finops-profile
export AWS_REGION=us-east-1

# Option B: explicit environment variables
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
```

### 3. Define your policy

Edit `config.yaml` to match your retention requirements:

```yaml
# Delete images older than this many days
retention_days: 30

# When true, only report what would be deleted without making changes
dry_run: true

# Repositories to skip during cleanup
excluded_repositories:
  - my-critical-service
  - shared-base-images
```

### 4. Preview (safe, no deletions)

```bash
python cleaner.py
```

This honors `dry_run: true` from the config and prints every image that would be removed.

### 5. Execute the cleanup

When you have reviewed the preview and are ready to delete:

```bash
python cleaner.py --force
```

### CLI reference

| Flag | Description |
| --- | --- |
| `--config PATH` | Path to the YAML config file (default: `config.yaml`). |
| `--retention-days N` | Override `retention_days` from the config file. |
| `--dry-run` | Force preview mode regardless of config. |
| `--force` | Perform real deletions, overriding `dry_run`. |

```bash
# Preview a tighter 7-day window
python cleaner.py --dry-run --retention-days 7

# Force a real cleanup with a 7-day window
python cleaner.py --force --retention-days 7
```

---

## Testing

The project ships with a unit-test suite that mocks the ECR API — no AWS account required.

```bash
pip install pytest
pytest -v
```

---

## Recommended Workflow

1. Start with `dry_run: true` and review the reported images.
2. Tune `retention_days` and `excluded_repositories` until the preview matches intent.
3. Run with `--force` manually, or schedule it (cron, CI, or a Lambda) once you trust the policy.

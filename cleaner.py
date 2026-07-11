import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ecr-cleaner")

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load the YAML configuration file."""
    with open(path, "r") as f:
        config = yaml.safe_load(f)
    logger.info("Loaded configuration from %s", path)
    return config


def get_stale_images(repo_name: str, retention_days: int, excluded_tags: list = None, ecr_client=None) -> list:
    """Return image digests in a repository that are untagged or older than retention_days.

    Protects critical production images containing strings defined in the excluded_tags list.

    Args:
        repo_name: Name of the ECR repository to scan.
        retention_days: Images pushed more than this many days ago are stale.
        excluded_tags: Optional list of regex pattern strings or static tags to preserve (e.g., ['prod', 'stable']).
        ecr_client: Optional boto3 ECR client; one is created if not provided.

    Returns:
        A list of imageDigest strings that should be cleaned up.
    """
    if ecr_client is None:
        ecr_client = boto3.client("ecr")

    excluded_tags = excluded_tags or []
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stale_digests = []

    paginator = ecr_client.get_paginator("describe_images")
    for page in paginator.paginate(repositoryName=repo_name):
        for image in page.get("imageDetails", []):
            digest = image.get("imageDigest")
            if digest is None:
                continue

            tags = image.get("imageTags", [])
            is_untagged = not tags
            pushed_at = image.get("imagePushedAt")
            is_expired = pushed_at is not None and pushed_at < cutoff

            # Check if any tag matches the protection list (e.g., contains 'prod', 'stable', etc.)
            is_protected = any(
                any(ex_tag in t.lower() for ex_tag in excluded_tags) for t in tags
            )

            if is_protected:
                logger.debug("Image %s in %s is protected by tag(s): %s. Skipping lifecycle evaluation.", digest, repo_name, tags)
                continue

            if is_untagged or is_expired:
                stale_digests.append(digest)

    logger.info(
        "Found %d stale image(s) in repository %s", len(stale_digests), repo_name
    )
    return stale_digests


def clean_repository(
    repo_name: str, stale_images: list, dry_run: bool, ecr_client=None
) -> int:
    """Delete stale images from a repository, or log them when dry_run is True.

    Args:
        repo_name: Name of the ECR repository to clean.
        stale_images: List of image digests to delete.
        dry_run: When True, only report what would be deleted.
        ecr_client: Optional boto3 ECR client; one is created if not provided.

    Returns:
        The number of images processed.
    """
    if not stale_images:
        logger.info("No stale images to process in repository %s", repo_name)
        return 0

    if dry_run:
        logger.info(
            "[DRY RUN] Would delete %d image(s) from repository %s",
            len(stale_images),
            repo_name,
        )
        for digest in stale_images:
            logger.info("[DRY RUN] Would delete %s", digest)
            print(f"[DRY RUN] {repo_name}: {digest}")
        return len(stale_images)

    if ecr_client is None:
        ecr_client = boto3.client("ecr")

    processed = 0
    # batch_delete_image accepts up to 100 image IDs per call.
    for start in range(0, len(stale_images), 100):
        batch = stale_images[start:start + 100]
        image_ids = [{"imageDigest": digest} for digest in batch]
        response = ecr_client.batch_delete_image(
            repositoryName=repo_name, imageIds=image_ids
        )

        deleted = response.get("imageIds", [])
        processed += len(deleted)
        for image in deleted:
            logger.info("Deleted %s from %s", image.get("imageDigest"), repo_name)

        for failure in response.get("failures", []):
            logger.error(
                "Failed to delete %s from %s: %s",
                failure.get("imageId", {}).get("imageDigest"),
                repo_name,
                failure.get("failureReason"),
            )

    logger.info("Deleted %d image(s) from repository %s", processed, repo_name)
    return processed


def parse_args(argv: list = None) -> argparse.Namespace:
    """Parse command-line arguments that can override config values."""
    parser = argparse.ArgumentParser(
        description="Clean up old or untagged AWS ECR images."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Path to the YAML config file (default: %(default)s).",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Override retention_days from the config file.",
    )
    dry_run_group = parser.add_mutually_exclusive_group()
    dry_run_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Only report what would be deleted (overrides config).",
    )
    dry_run_group.add_argument(
        "--force",
        dest="dry_run",
        action="store_false",
        help="Actually delete stale images (overrides config dry_run).",
    )
    return parser.parse_args(argv)


def main(argv: list = None) -> None:
    args = parse_args(argv)
    config = load_config(args.config)

    retention_days = (
        args.retention_days
        if args.retention_days is not None
        else config.get("retention_days", 30)
    )
    dry_run = args.dry_run if args.dry_run is not None else config.get("dry_run", True)
    excluded_repositories = config.get("excluded_repositories", []) or []
    excluded_tags = config.get("excluded_tags", []) or []

    logger.info("Retention period: %s days", retention_days)
    logger.info("Dry run: %s", dry_run)
    logger.info("Excluded repositories: %s", excluded_repositories)
    logger.info("Excluded/Protected image tags: %s", excluded_tags)

    ecr_client = boto3.client("ecr")
    logger.info("Initialized boto3 ECR client in region %s", ecr_client.meta.region_name)

    excluded = set(excluded_repositories)
    total_processed = 0

    paginator = ecr_client.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for repo in page.get("repositories", []):
            repo_name = repo.get("repositoryName")
            if repo_name in excluded:
                logger.info("Skipping excluded repository %s", repo_name)
                continue

            stale_images = get_stale_images(repo_name, retention_days, excluded_tags, ecr_client)
            total_processed += clean_repository(
                repo_name, stale_images, dry_run, ecr_client
            )

    logger.info(
        "Done. Processed %d image(s) across all repositories (dry_run=%s)",
        total_processed,
        dry_run,
    )


if __name__ == "__main__":
    main()

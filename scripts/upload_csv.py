#!/usr/bin/env python3
"""Upload the crowd feedback CSV to S3 for the Lambda agent.

Default object key matches infra/stack.py S3_FEEDBACK_KEY (geo-signals/feedback.csv).

Examples:
  python scripts/upload_csv.py data-assets/feedback.csv YOUR_BUCKET
  S3_KEY=geo-signals/feedback.csv python scripts/upload_csv.py feedback-09052026.csv YOUR_BUCKET
"""
import os
import sys

import boto3


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: python scripts/upload_csv.py <csv_path> <bucket_name>\n"
            "Env: S3_KEY (default geo-signals/feedback.csv)",
            file=sys.stderr,
        )
        sys.exit(1)

    csv_path = sys.argv[1]
    bucket = sys.argv[2]
    s3_key = os.environ.get("S3_KEY", "geo-signals/feedback.csv")

    s3 = boto3.client("s3")
    with open(csv_path, "rb") as fh:
        s3.put_object(Bucket=bucket, Key=s3_key, Body=fh, ContentType="text/csv")

    print(f"Uploaded {csv_path} → s3://{bucket}/{s3_key}")


if __name__ == "__main__":
    main()

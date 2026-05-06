#!/usr/bin/env python3
"""One-time script: upload the geopolitics-dump.csv to S3."""
import os
import sys

import boto3


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python upload_csv.py <csv_path> <bucket_name>")
        sys.exit(1)

    csv_path = sys.argv[1]
    bucket = sys.argv[2]
    s3_key = os.environ.get("S3_KEY", "geo-signals/geopolitics-dump.csv")

    s3 = boto3.client("s3")
    with open(csv_path, "rb") as fh:
        s3.put_object(Bucket=bucket, Key=s3_key, Body=fh, ContentType="text/csv")

    print(f"Uploaded {csv_path} → s3://{bucket}/{s3_key}")


if __name__ == "__main__":
    main()

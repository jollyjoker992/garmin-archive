#!/bin/sh
# Create an IAM user that can use ONE bucket only, and write its access key to
# docker/secrets.env (mode 600) for the container. The key is never printed.
#
#   docker/create-s3-key.sh <bucket> [aws-profile] [iam-user-name] [rclone-remote]
#
# Needs IAM permissions on the profile (iam:CreateUser, iam:PutUserPolicy,
# iam:CreateAccessKey). Re-running for the same user adds a second key (IAM
# allows two); delete old keys in the IAM console.
set -eu
BUCKET=${1:?usage: docker/create-s3-key.sh <bucket> [aws-profile] [iam-user] [rclone-remote]}
PROFILE=${2:-default}
IAM_USER=${3:-garmin-archive-backup}
REMOTE=$(printf '%s' "${4:-s3garmin}" | tr '[:lower:]-' '[:upper:]_')
OUT="$(cd "$(dirname "$0")" && pwd)/secrets.env"

POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
     "Resource": "arn:aws:s3:::$BUCKET"},
    {"Effect": "Allow",
     "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:RestoreObject",
                "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
     "Resource": "arn:aws:s3:::$BUCKET/*"}
  ]
}
JSON
)

if ! aws iam get-user --user-name "$IAM_USER" --profile "$PROFILE" >/dev/null 2>&1; then
  aws iam create-user --user-name "$IAM_USER" --profile "$PROFILE" >/dev/null
  echo "Created IAM user $IAM_USER"
fi
aws iam put-user-policy --user-name "$IAM_USER" --policy-name "garmin-archive-$BUCKET" \
  --policy-document "$POLICY" --profile "$PROFILE"
echo "Policy: only s3://$BUCKET"

KEY=$(aws iam create-access-key --user-name "$IAM_USER" --profile "$PROFILE" \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
umask 077
{
  echo "# S3 credentials for the garmin-archive container (IAM user $IAM_USER, bucket $BUCKET)."
  echo "# Created by docker/create-s3-key.sh. Never commit this file."
  echo "RCLONE_CONFIG_${REMOTE}_ENV_AUTH=false"
  echo "RCLONE_CONFIG_${REMOTE}_PROFILE="
  echo "RCLONE_CONFIG_${REMOTE}_ACCESS_KEY_ID=$(printf '%s' "$KEY" | cut -f1)"
  echo "RCLONE_CONFIG_${REMOTE}_SECRET_ACCESS_KEY=$(printf '%s' "$KEY" | cut -f2)"
} > "$OUT"
chmod 600 "$OUT"
echo "Wrote $OUT (mode 600). New IAM keys can take ~10 s to become valid."

# 62821 m101 Door Review

Private entry: `/door-review`; protected by the existing invitation/email login.
The sample-review algorithm navigation links to this project.

Scope: `source_device=62821`, `source_kind=door-state`, main JPG captures matching
`ch<channel>_m101_<sequence>.jpg`. Channels come from capture metadata, not a fixed
allowlist or a write to device configuration. Other devices, m103/m104 and
thumbnails are excluded. Unlabelled captures are visible as pending.

Review records contain independent `open/closed/uncertain` image state and
`correct/false_alarm/uncertain` event verdicts. Notes are not required or shown
in the review form; existing notes are preserved when updating a review.
A single image is not proof of a door transition. Current device baselines are
not historical before-event evidence.

`door_review_revisions` retains immutable revisions, source image hashes,
reviewer identity and idempotency receipts. It deliberately does not write YOLO
bounding-box truth, change AI/person-training labels, enable publication, send
customer alarms or delete images. `scene_change` stays catalog-only in the
visual-task registry until a separate temporal training/export contract is agreed.

## Collection

`collect_door_review.py --known-hosts <trusted-known-hosts>` uses environment-only
device credentials and Paramiko RejectPolicy. The mapped endpoint is
`42.193.140.103:62955`; verify its current catalog identity and independently
confirm its SSH key before installing any known-host entry. A key obtained from
ssh-keyscan alone is not trusted. Never switch to AutoAddPolicy.

Run only inside the existing Server-8 review image with the systemd
EnvironmentFile `/etc/ai-bot-sample-review/device.env` and optional `oss.env`.
Pass environment variable names into Docker, not credential values. Mount the
trusted known-host file read-only and use the deployed release's collector.

The collector reuses the review sync lock, ingestion key, content deduplication
and configured OSS backend. It imports at most 100 captures per invocation,
looks back 14 days, preserves an 8 GiB free-space floor and enforces existing
image/item storage limits. It does not invoke retention sweeps, deployment,
device commands, service restarts or channel changes. `--dry-run` only lists
eligible remote files. No collection timer is enabled by this feature release.

An empty project is not evidence of healthy detection. Until a trusted SSH read
and actual sample import have succeeded, capture ingestion remains unverified.

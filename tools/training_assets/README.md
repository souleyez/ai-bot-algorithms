# Exact training materialization

`materialize_manifest.py` consumes only an approved Platform manifest revision,
its cursor-paged frozen members, and AI-BOT's exact `review-fact` and `original`
resolvers. It never reads a current review row or a device directory.

Both bearer values are supplied through root-readable files. The class mapping
is a bounded JSON object such as `{"courier":0}` whose canonical SHA-256 must
equal the frozen manifest's `class_mapping_digest`. Output is staged outside the
target and atomically renamed only after every revision, fact, image, taxonomy,
split and digest check succeeds.

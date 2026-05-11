# AI-BOT Algorithm Catalog

This folder stores the public, non-secret catalog inputs for the private AI-BOT algorithm platform.

- `devices.json`: known small-device pool. It stores machine codes and mapped ports only, never passwords.
- `recommended-artifacts.json`: current approved algorithm artifacts. Paths are relative to the local package root, usually `~/Desktop/算法包`.

Large binaries stay outside GitHub:

- `.ai`
- `.zip`
- `.rknn`
- training datasets
- customer images or captures

Use `tools/algorithm_platform/catalog_mvp.py` to build a private runtime catalog and copy artifacts into an ignored runtime folder before syncing them to the server.

Use `tools/algorithm_platform/probe_device.py` to generate read-only device inventory state after the runtime catalog exists. SSH credentials must come from environment variables, not checked-in metadata.

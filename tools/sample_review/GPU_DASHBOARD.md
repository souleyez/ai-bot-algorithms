# Public GPU dashboard

The public overview shows only allowlisted resource metrics, model basenames and
fixed service labels. It never publishes addresses, process arguments, model
paths, prompts, users, tokens or SSH credentials. Model inventory means installed
weights/components, not GPU residency. Serving status requires an active service
and a successful local health check. ComfyUI queue counts do not expose job data.

## Collection

- Server 8 runs `ai-bot-gpu-dashboard-server8.timer` every 60 seconds.
- Its dedicated key at `/etc/ai-bot-gpu-dashboard/id_ed25519` can only invoke
  `/usr/bin/python3 /opt/ai-bot-gpu-monitor/current/gpu_dashboard.py` on server 1.
- The server-1 authorized-key entry uses `restrict`, an exact forced command and
  a source-address restriction. No shell, PTY or forwarding is granted.
- Both hops require strict host-key validation. Server 1 reuses its existing
  authorized SSH routes to the two GPU hosts. No keys are copied from them.
- The probe is streamed to Python stdin and only reads GPU counters, host
  counters, model metadata, exact systemd states and loopback health/queue APIs.
  It never starts inference or modifies files on GPU hosts.
- Server 8 atomically updates
  `/srv/ai-bot-sample-review/data/gpu-dashboard.json`. A transport failure retains
  the previous timestamp. The UI marks snapshots older than three minutes stale;
  a failed host is unknown/unreachable, never zero utilization.
- The monitoring unit is separate from gateway collection and model workloads.
  Stopping/disabling this timer does not affect either GPU service.

## Deployment

Include the probe, collector and timer in the normal committed Server-8 release.
On server 1 install the same two Python files in an immutable version directory
under `/opt/ai-bot-gpu-monitor/releases/`, with `current` pointing there.
Generate the dedicated Ed25519 key on server 8, without transferring the private
key. Provision server-1 ED25519 host trust through an already verified SSH session,
not unverified keyscan. Install the restricted public key on server 1 and verify
that a client-supplied command still returns only the fixed snapshot.

On server 8 install the two `ai-bot-gpu-dashboard-server8` systemd units, run one
collection, then enable the timer. Check both nodes, health, snapshot timestamp,
public API sanitization, login boundaries and desktop/mobile rendering. No GPU
model service restart or deployment is part of this procedure.

#!/usr/bin/env python3
"""Read-only, standard-library GPU probe streamed over SSH; no credentials read."""

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import build_opener, ProxyHandler


def command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=8, check=False)
    if result.returncode:
        raise ValueError("command_failed")
    return result.stdout


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def gpu_metrics(raw):
    fields = ("utilization", "memoryUsedMiB", "memoryTotalMiB", "temperatureC", "powerW", "powerLimitW")
    result = []
    for row in csv.reader(io.StringIO(raw), skipinitialspace=True):
        if len(row) == 9:
            result.append({"index": int(row[0]), "name": row[1], "driver": row[2],
                           **dict(zip(fields, (numeric(value) for value in row[3:])))})
    return result


def cpu_sample():
    values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def host_metrics():
    before = cpu_sample()
    time.sleep(0.25)
    after = cpu_sample()
    elapsed = after[0] - before[0]
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) / 1024
    disk = shutil.disk_usage("/")
    return {
        "cpuPercent": round(100 * (1 - (after[1] - before[1]) / elapsed), 1) if elapsed else None,
        "cpuCount": os.cpu_count(), "load1": os.getloadavg()[0],
        "memoryUsedMiB": round(memory["MemTotal"] - memory["MemAvailable"]),
        "memoryTotalMiB": round(memory["MemTotal"]),
        "diskUsedGiB": round(disk.used / 1024 ** 3, 1),
        "diskTotalGiB": round(disk.total / 1024 ** 3, 1),
        "uptimeSeconds": int(float(Path("/proc/uptime").read_text().split()[0])),
    }


def api(port, path):
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:%d%s" % (port, path), timeout=3) as response:
        return json.loads(response.read(1024 * 1024))


def service(unit, label, port, kind):
    result = {"name": label, "state": "unknown", "healthy": None, "model": "",
              "queueRunning": None, "queuePending": None, "contextSize": None}
    try:
        raw = command(["systemctl", "show", unit, "--property=ActiveState", "--property=MainPID"])
        properties = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        result["state"] = properties.get("ActiveState", "unknown")
        if result["state"] != "active":
            return result
        if kind == "llama":
            # Never expose command lines: extract only the model basename and context size.
            pid = int(properties.get("MainPID", "0"))
            args = Path("/proc/%d/cmdline" % pid).read_bytes().decode().split("\0")
            for index, value in enumerate(args[:-1]):
                if value in ("--model", "-m"):
                    result["model"] = Path(args[index + 1]).name
                if value in ("--ctx-size", "-c"):
                    result["contextSize"] = numeric(args[index + 1])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if result["state"] == "active":
        try:
            health = api(port, "/system_stats" if kind == "comfy" else "/health")
            result["healthy"] = isinstance(health, dict) and (
                "system" in health if kind == "comfy" else health.get("status") == "ok")
        except Exception:
            result["healthy"] = False
        if kind == "comfy":
            try:
                queue = api(port, "/queue")
                result["queueRunning"] = len(queue["queue_running"])
                result["queuePending"] = len(queue["queue_pending"])
            except Exception:
                pass
    return result


def model_inventory(roots):
    found = {}
    extensions = {".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin"}
    for root in roots:
        if not root.is_dir():
            continue
        for directory, _, files in os.walk(root):
            for name in files:
                path = Path(directory) / name
                if path.suffix.lower() not in extensions:
                    continue
                try:
                    resolved = str(path.resolve())
                    size = path.stat().st_size
                    if size > 50 * 1024 * 1024:
                        found[resolved] = {"name": name, "sizeMiB": round(size / 1024 ** 2)}
                except OSError:
                    continue
    return sorted(found.values(), key=lambda item: item["name"].lower())[:100]


def probe(profile):
    result = {"reachable": True, "observedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "gpus": [], "host": {}, "services": [], "models": [], "errors": []}
    try:
        result["gpus"] = gpu_metrics(command([
            "nvidia-smi", "--query-gpu=index,name,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits"]))
        if not result["gpus"]:
            result["errors"].append("gpu_unavailable")
    except (OSError, ValueError, subprocess.SubprocessError):
        result["errors"].append("gpu_unavailable")
    try:
        result["host"] = host_metrics()
    except (OSError, ValueError, KeyError):
        result["errors"].append("host_unavailable")
    if profile == "h3":
        result["services"] = [service("comfyui-h3.service", "ComfyUI / H3", 8188, "comfy")]
        roots = [Path("/srv/h3/models"), Path("/srv/h3/ComfyUI/models")]
    else:
        result["services"] = [
            service("llama-qwen38-obliterated.service", "Qwen3.8 27B", 8080, "llama"),
            service("llama-qwen3-embedding-4b.service", "Qwen3 Embedding 4B", 8081, "llama"),
        ]
        roots = [Path("/data/models")]
    try:
        result["models"] = model_inventory(roots)
    except OSError:
        result["errors"].append("inventory_unavailable")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=("h3", "souleye"))
    print(json.dumps(probe(parser.parse_args().profile), ensure_ascii=True))

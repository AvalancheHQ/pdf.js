#!/usr/bin/env python3
"""Run several variance variants sequentially on one long-lived runner.

Every sample is a fresh `codspeed run --skip-upload` process with its own profile
folder, run with the variant commit checked out (detached HEAD; variants differ
only in `.variance/env`). The variant knobs are set only in that subprocess
environment. After each sample, its compact results, logs and manifest are persisted;
then, if VARIANCE_UPLOAD_TOKEN is set, the raw profile folder is uploaded to staging
as an independent Local run (`--skip-run --skip-setup`, token only in that process),
before the next sample starts. Large profiling files are not archived (listed in the
manifest). Run this script from a copy outside the checkout.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

KNOB_LINE = re.compile(
    r"^(VARIANT|CODSPEED_WALLTIME_FORKS|CODSPEED_WALLTIME_GC_BETWEEN_ROUNDS|VARIANCE_[A-Z_]+)=[A-Za-z0-9_.-]*$"
)
KNOB_KEY = re.compile(
    r"^(CODSPEED_WALLTIME_[A-Z_]+|VARIANCE_[A-Z_]+|VARIANT|CODSPEED_TOKEN|CODSPEED_OAUTH_TOKEN|CODSPEED_PROFILER_ENABLED)$"
)
API_URL = "https://gql.staging.preview.codspeed.io"
UPLOAD_URL = "https://api.staging.preview.codspeed.io/upload"
REPOSITORY = "AvalancheHQ/pdf.js"
RUN_URL = re.compile(r"https?://\S*/runs/[0-9a-f]{24}\b")
LAST_VARIANT = "baseline2"
# Profile files above this size are not archived (perf data, jit dumps).
MAX_KEPT_FILE = 32 * 1024 * 1024
BENCH_CMD = ["npx", "vitest", "bench", "--run", "--config", "test/benchmarks/vitest.config.mjs"]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_knobs(sha):
    text = subprocess.run(
        ["git", "show", f"{sha}:.variance/env"], capture_output=True, text=True, check=True
    ).stdout
    knobs = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if not KNOB_LINE.match(line):
            sys.exit(f"unsupported line in {sha}:.variance/env: {line!r}")
        key, value = line.split("=", 1)
        knobs[key] = value
    return knobs


def schedule(variants, shard, reps):
    """(variant, rep, sample_index) in run order: rotated variants per rep, LAST_VARIANT last."""
    names = [v for v in variants if v != LAST_VARIANT]
    order = []
    for rep in range(1, reps + 1):
        index = (shard - 1) * reps + rep
        k = (index - 1) % len(names) if names else 0
        order += [(v, rep, index) for v in names[k:] + names[:k]]
    if LAST_VARIANT in variants:
        order += [(LAST_VARIANT, rep, (shard - 1) * reps + rep) for rep in range(1, reps + 1)]
    return order


def own_tmp_files(since):
    """JIT dumps and perf maps written by this user since `since` (runner leftovers)."""
    uid = os.getuid()
    found = []
    for pattern in ("jit-*.dump", "perf-*.map"):
        for p in Path("/tmp").glob(pattern):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_uid == uid and st.st_mtime >= since:
                found.append(p)
    return found


def archive_profile(profile, dest):
    """Copy the profile folder without large files; return the dropped files."""
    dropped = []
    if not profile.is_dir():
        return dropped
    for src in sorted(profile.rglob("*")):
        if not src.is_file() or src.is_symlink():
            continue
        rel = src.relative_to(profile)
        size = src.stat().st_size
        if size > MAX_KEPT_FILE and not rel.parts[0] == "results":
            dropped.append({"path": str(rel), "bytes": size})
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
    return dropped


def bench_count(results_dir):
    uris = set()
    for f in results_dir.glob("*.json"):
        try:
            uris.update(b["uri"] for b in json.loads(f.read_text()).get("benchmarks", []))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    return len(uris)


def upload(profile, sample_dir, env):
    """Upload one profile folder as an independent Local run on staging; never raises."""
    cmd = [
        "codspeed", "--api-url", API_URL, "run", "--repository", REPOSITORY, "--upload-url", UPLOAD_URL,
        "--mode", "walltime", "--profile-folder", str(profile), "--skip-run", "--skip-setup", "--", "true",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
        code, output = proc.returncode, proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        code, output = None, f"{type(e).__name__}: {e}\n"
    (sample_dir / "upload.log").write_text(output)
    print(output, end="", flush=True)
    urls = RUN_URL.findall(output)
    run_url = urls[-1] if urls else ""
    error = f"upload exited with {code}" if code != 0 else ""
    if not error and not run_url:
        error = "upload returned no CodSpeed run URL"
    return {
        "exit": code,
        "run_url": run_url,
        "run_id": run_url.rsplit("/", 1)[-1] if run_url else "",
        "seconds": round(time.monotonic() - started, 1),
        "error": error,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch", required=True)
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--reps", type=int, required=True)
    p.add_argument("--variants", required=True, help="comma-separated names")
    p.add_argument("--config", required=True)
    p.add_argument("--patch", required=True, help="the plugin patch applied by the workflow")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True, help="scratch dir for raw profile folders")
    p.add_argument("--timeout-minutes", type=int, default=120)
    p.add_argument("--runner-version", default="")
    p.add_argument("--plugin-ref", default="")
    args = p.parse_args()

    config = json.loads(Path(args.config).read_text())
    known = {v["name"]: v for v in config["variants"]}
    variants = [v for v in args.variants.split(",") if v]
    unknown = [v for v in variants if v not in known]
    if unknown or not variants or len(set(variants)) != len(variants):
        sys.exit(f"bad --variants {args.variants!r} (known: {', '.join(known)})")
    knobs = {}
    for name in variants:
        knobs[name] = load_knobs(known[name]["sha"])
        if knobs[name].get("VARIANT") != name:
            sys.exit(f"{known[name]['sha']}:.variance/env has VARIANT={knobs[name].get('VARIANT')!r}, expected {name}")

    # The environment of every sample: the job environment without any knob or token, plus the variant's knobs.
    base_env = {k: v for k, v in os.environ.items() if not KNOB_KEY.match(k)}
    token = os.environ.get("VARIANCE_UPLOAD_TOKEN", "")
    upload_env = {k: v for k, v in base_env.items() if k != "GITHUB_ACTIONS"}
    # The staging user JWT: upload token and API login of the Local provider (upload process only).
    upload_env["CODSPEED_TOKEN"] = token
    upload_env["CODSPEED_OAUTH_TOKEN"] = token
    harness_sha = sh("git", "rev-parse", "HEAD")
    host = {
        "hostname": socket.gethostname(),
        "runner_name": os.environ.get("RUNNER_NAME", ""),
        "cpu_model": next(
            (l.split(":", 1)[1].strip() for l in sh("lscpu").splitlines() if l.startswith("Model name:")), ""
        ),
        "kernel": platform.release(),
        "node_version": sh("node", "--version"),
        "codspeed_version": sh("codspeed", "--version"),
        "runner_version": args.runner_version,
        "plugin_ref": args.plugin_ref,
        "plugin_patch_sha256": sha256(args.patch),
        "harness_sha": harness_sha,
        "gh_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "gh_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "gh_job": os.environ.get("GITHUB_JOB", ""),
    }
    print(json.dumps(host, indent=2), flush=True)

    plan = schedule(variants, args.shard, args.reps)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"plan-{args.shard:02d}.json").write_text(
        json.dumps([{"variant": v, "rep": r, "sample_index": i} for v, r, i in plan], indent=1)
    )
    manifest_all = args.out / f"manifest-{args.shard:02d}.jsonl"
    print(f"upload: {'staging Local runs' if token else 'disabled'}", flush=True)
    failed = 0
    for n, (variant, rep, index) in enumerate(plan, 1):
        sample_dir = args.out / "samples" / variant / f"{index:03d}"
        if sample_dir.exists():
            sys.exit(f"{sample_dir} already exists: overlapping sample index")
        sample_dir.mkdir(parents=True)
        profile = args.work / f"profile-{variant}-{index:03d}"
        shutil.rmtree(profile, ignore_errors=True)
        profile.mkdir(parents=True)
        sha = known[variant]["sha"]
        # The Local upload takes the commit from HEAD: measure and upload with the variant commit checked out.
        subprocess.run(["git", "checkout", "--quiet", "--detach", sha], check=True)
        if sh("git", "rev-parse", "HEAD") != sha:
            sys.exit(f"HEAD is not {sha} after checkout")
        env = {**base_env, **knobs[variant]}
        cmd = [
            "timeout", "--kill-after=60", f"{args.timeout_minutes}m",
            "codspeed", "run", "--skip-upload", "--mode", "walltime", "--profile-folder", str(profile), "--",
            *BENCH_CMD,
        ]
        print(f"::group::[{n}/{len(plan)}] {variant} sample {index} (rep {rep})", flush=True)
        started_at, started, wall0 = now(), time.monotonic(), time.time()
        with open(sample_dir / "run.log", "wb") as log:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                log.write(chunk)
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
            code = proc.wait()
        seconds = round(time.monotonic() - started, 1)
        ended_at = now()

        dropped = archive_profile(profile, sample_dir / "profile")
        count = bench_count(sample_dir / "profile" / "results")
        error = ""
        if code != 0:
            error = f"codspeed run exited with {code}"
        elif count == 0:
            error = "no benchmark results"
        if error:
            failed += 1
            print(f"::error::{variant} sample {index}: {error}", flush=True)
        removed = []
        row = {
            "batch": args.batch,
            "variant": variant,
            "variant_sha": sha,
            "knobs": knobs[variant],
            "shard": args.shard,
            "rep": rep,
            "sample_index": index,
            "position_in_worker": n,
            "host": host["hostname"],
            **{k: v for k, v in host.items() if k != "hostname"},
            "start": started_at,
            "end": ended_at,
            "seconds": seconds,
            "exit": code,
            "bench_count": count,
            "error": error,
            "profile_path": str(sample_dir.relative_to(args.out) / "profile"),
            "dropped_profile_files": dropped,
            "removed_tmp_files": removed,
            "upload": None,
        }
        # Raw data is on disk before the upload is attempted.
        (sample_dir / "manifest.json").write_text(json.dumps(row, indent=2))
        if token and not error:
            row["upload"] = upload(profile, sample_dir, upload_env)
            if row["upload"]["error"]:
                failed += 1
                print(f"::error::{variant} sample {index}: {row['upload']['error']}", flush=True)
        for f in own_tmp_files(wall0):
            try:
                removed.append({"path": str(f), "bytes": f.stat().st_size})
                f.unlink()
            except OSError:
                pass
        (sample_dir / "manifest.json").write_text(json.dumps(row, indent=2))
        shutil.rmtree(profile, ignore_errors=True)
        with open(manifest_all, "a") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        print(json.dumps({k: v for k, v in row.items()
                          if k not in {"dropped_profile_files", "removed_tmp_files"}}), flush=True)
        print("::endgroup::", flush=True)

    subprocess.run(["git", "checkout", "--quiet", "--detach", harness_sha], check=False)
    print(f"{failed} of {len(plan)} samples failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

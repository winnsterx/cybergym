#!/usr/bin/env python3
"""
Deploy CTF challenge containers to GCP Compute Engine.

Usage:
    # Deploy mra challenge
    uv run python scripts/deploy_gcp.py deploy --challenge mra

    # Deploy with custom name
    uv run python scripts/deploy_gcp.py deploy --challenge mra --name my-mra-instance

    # List running challenges
    uv run python scripts/deploy_gcp.py list

    # Tear down
    uv run python scripts/deploy_gcp.py destroy --name mra-challenge

Prerequisites:
    1. gcloud CLI installed and authenticated: gcloud auth application-default login
    2. pip install google-cloud-compute
    3. Set your project: gcloud config set project YOUR_PROJECT_ID

  2. How GCP's create-with-container works

  ┌─────────────────────────────────────────────┐
  │  GCP VM (Container-Optimized OS)            │
  │  ┌───────────────────────────────────────┐  │
  │  │  Docker Container (archiveooo/pub:mra)│  │
  │  │  ┌─────────────────────────────────┐  │  │
  │  │  │  xinetd listening on port 8000  │  │  │
  │  │  │  └─> forks /wrapper per conn    │  │  │
  │  │  │      └─> qemu-aarch64 /mra      │  │  │
  │  │  └─────────────────────────────────┘  │  │
  │  └───────────────────────────────────────┘  │
  │                    ↑                        │
  │              host networking                │
  │              (port 8000 exposed)            │
  └─────────────────────────────────────────────┘
                      ↑
           External IP: 34.69.244.58:8000
                      ↑
                nc 34.69.244.58 8000
"""

import argparse
import csv
import socket
import subprocess
import sys
import time
from pathlib import Path

# Path to defcon-ooo metadata
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "cybergym_data" / "data" / "defcon-ooo"
METADATA_CSV = DATA_DIR / "defcon-ooo-metadata.csv"
ANSWERS_CSV = DATA_DIR / "answers.csv"

# Challenges that need to be built from local Dockerfiles (no remote registry image)
# Format: challenge_name -> (dockerfile_dir, port)
LOCAL_CHALLENGES: dict[str, tuple[Path, int]] = {
    "election_coin": (
        PROJECT_ROOT.parent / "dc2019q-election_coin" / "service",
        8888,
    ),
    "chainedrsa": (
        PROJECT_ROOT.parent / "dc2019q-chainedrsa" / "service",
        5000,
    ),
    "gloryhost": (
        PROJECT_ROOT.parent / "dc2019q-gloryhost" / "service",
        9999,
    ),
}


def load_challenges() -> dict[str, tuple[str, int]]:
    """Load challenges from CSV files.

    Only includes challenges that have both:
    - A docker_image in metadata.csv
    - A non-empty flag in answers.csv

    Returns:
        dict mapping challenge name -> (docker_image, port)
    """
    # Load answers to check which challenges have flags
    tasks_with_flags = set()
    if ANSWERS_CSV.exists():
        with open(ANSWERS_CSV) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("flag", "").strip():
                    tasks_with_flags.add(row["task"])

    # Load metadata and filter
    challenges = {}
    if METADATA_CSV.exists():
        with open(METADATA_CSV) as f:
            reader = csv.DictReader(f)
            for row in reader:
                task_id = row.get("task", "")
                docker_image = row.get("docker_image", "").strip()
                port = row.get("port", "").strip()

                # Only include if has docker image, port, and a flag
                if docker_image and port and task_id in tasks_with_flags:
                    # Extract short name from "defcon-ooo:name"
                    short_name = task_id.split(":")[-1] if ":" in task_id else task_id
                    challenges[short_name] = (docker_image, int(port))

    return challenges


# Load challenges dynamically from CSV
CHALLENGES = load_challenges()

DEFAULT_ZONE = "us-central1-a"
DEFAULT_MACHINE_TYPE = "e2-micro"


def run_cmd(cmd: list[str], check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a command."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=True, text=True, cwd=cwd)


def get_project_id() -> str:
    """Get current GCP project ID."""
    result = run_cmd(["gcloud", "config", "get-value", "project"])
    project = result.stdout.strip()
    if not project:
        print("Error: No GCP project set. Run: gcloud config set project YOUR_PROJECT_ID")
        sys.exit(1)
    return project


def wait_for_port(host: str, port: int, timeout: int = 300, interval: int = 5) -> bool:
    """Wait until a TCP port is accepting connections.

    Args:
        host: The host to connect to
        port: The port to check
        timeout: Maximum time to wait in seconds (default 5 minutes)
        interval: Time between checks in seconds

    Returns:
        True if port is reachable, False if timeout exceeded
    """
    start_time = time.time()
    attempt = 0
    while time.time() - start_time < timeout:
        attempt += 1
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect((host, port))
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            elapsed = int(time.time() - start_time)
            print(f"  Waiting for service... (attempt {attempt}, {elapsed}s elapsed)")
            time.sleep(interval)
    return False


def build_and_push_image(challenge: str, dockerfile_dir: Path, project_id: str) -> str:
    """Build a Docker image from local Dockerfile and push to GCR.

    Args:
        challenge: Challenge name (used for image tag)
        dockerfile_dir: Directory containing the Dockerfile
        project_id: GCP project ID

    Returns:
        The full GCR image URL
    """
    image_url = f"gcr.io/{project_id}/{challenge}:latest"

    print(f"\n=== Building Docker image from {dockerfile_dir} ===")

    if not dockerfile_dir.exists():
        print(f"Error: Dockerfile directory not found: {dockerfile_dir}")
        sys.exit(1)

    if not (dockerfile_dir / "Dockerfile").exists():
        print(f"Error: No Dockerfile in {dockerfile_dir}")
        sys.exit(1)

    # Build the image
    print(f"\nBuilding image: {image_url}")
    result = run_cmd(
        ["docker", "build", "-t", image_url, "."],
        check=False,
        cwd=dockerfile_dir,
    )

    if result.returncode != 0:
        print(f"Error building Docker image:\n{result.stderr}")
        sys.exit(1)

    print(result.stdout)

    # Push to GCR
    print(f"\nPushing image to GCR...")
    result = run_cmd(["docker", "push", image_url], check=False)

    if result.returncode != 0:
        print(f"Error pushing image to GCR:\n{result.stderr}")
        print("\nMake sure you have authenticated Docker with GCR:")
        print("  gcloud auth configure-docker")
        sys.exit(1)

    print(result.stdout)
    print(f"Image pushed: {image_url}")

    return image_url


def ensure_firewall_rule(port: int):
    """Create firewall rule if it doesn't exist."""
    rule_name = f"allow-ctf-{port}"

    # Check if rule exists
    result = run_cmd(
        ["gcloud", "compute", "firewall-rules", "describe", rule_name],
        check=False
    )

    if result.returncode != 0:
        print(f"Creating firewall rule for port {port}...")
        run_cmd([
            "gcloud", "compute", "firewall-rules", "create", rule_name,
            f"--allow=tcp:{port}",
            "--target-tags=ctf-challenge",
            "--description=Allow CTF challenge traffic"
        ])
    else:
        print(f"Firewall rule {rule_name} already exists")


def deploy_challenge(
    challenge: str,
    name: str | None = None,
    zone: str = DEFAULT_ZONE,
    machine_type: str = DEFAULT_MACHINE_TYPE,
) -> tuple[str, int]:
    """
    Deploy a CTF challenge container to GCP.

    Returns (external_ip, port) for netcat connection.
    """
    # Check if it's a local challenge that needs building
    if challenge in LOCAL_CHALLENGES:
        dockerfile_dir, port = LOCAL_CHALLENGES[challenge]
        project_id = get_project_id()
        image = build_and_push_image(challenge, dockerfile_dir, project_id)
    elif challenge in CHALLENGES:
        image, port = CHALLENGES[challenge]
    else:
        all_challenges = set(CHALLENGES.keys()) | set(LOCAL_CHALLENGES.keys())
        print(f"Unknown challenge: {challenge}")
        print(f"Available: {', '.join(sorted(all_challenges))}")
        sys.exit(1)
    # GCP instance names must be lowercase and match regex [a-z]([-a-z0-9]*[a-z0-9])?
    # Replace underscores with hyphens
    instance_name = (name or f"{challenge}-challenge").lower().replace("_", "-")

    print(f"\n=== Deploying {challenge} ===")
    print(f"Image: {image}")
    print(f"Port: {port}")
    print(f"Instance: {instance_name}")
    print(f"Zone: {zone}")
    print()

    # Ensure firewall rule exists
    ensure_firewall_rule(port)

    # Create the VM with container
    print(f"\nCreating VM instance...")
    result = run_cmd([
        "gcloud", "compute", "instances", "create-with-container", instance_name,
        f"--container-image={image}",
        f"--machine-type={machine_type}",
        f"--zone={zone}",
        "--tags=ctf-challenge",
        "--container-restart-policy=always",
    ], check=False)

    if result.returncode != 0:
        if "already exists" in result.stderr:
            print(f"Instance {instance_name} already exists")
        else:
            print(f"Error creating instance: {result.stderr}")
            sys.exit(1)

    # Get external IP
    print("\nGetting external IP...")
    result = run_cmd([
        "gcloud", "compute", "instances", "describe", instance_name,
        f"--zone={zone}",
        "--format=get(networkInterfaces[0].accessConfigs[0].natIP)"
    ])

    external_ip = result.stdout.strip()

    # Wait for the service to be ready
    print(f"\nWaiting for container to start and port {port} to be ready...")
    print(f"  (This may take a few minutes while the Docker image is pulled)")
    if wait_for_port(external_ip, port):
        print("\n" + "=" * 50)
        print(f"Challenge deployed and ready!")
        print(f"  Connect with: nc {external_ip} {port}")
        print("=" * 50 + "\n")
    else:
        print("\n" + "=" * 50)
        print(f"WARNING: Timeout waiting for port {port} to be ready")
        print(f"  The container may still be starting. Try:")
        print(f"  nc {external_ip} {port}")
        print("=" * 50 + "\n")

    return external_ip, port


def list_challenges(zone: str = DEFAULT_ZONE):
    """List running CTF challenge instances."""
    print("\n=== Running CTF Challenges ===\n")

    result = run_cmd([
        "gcloud", "compute", "instances", "list",
        "--filter=tags.items=ctf-challenge",
        "--format=table(name,zone,status,networkInterfaces[0].accessConfigs[0].natIP:label=EXTERNAL_IP)"
    ])

    print(result.stdout)


def destroy_challenge(name: str, zone: str = DEFAULT_ZONE):
    """Delete a CTF challenge instance."""
    print(f"\n=== Destroying {name} ===\n")

    run_cmd([
        "gcloud", "compute", "instances", "delete", name,
        f"--zone={zone}",
        "--quiet"
    ])

    print(f"\n✓ Instance {name} deleted")


def main():
    parser = argparse.ArgumentParser(description="Deploy CTF challenges to GCP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Deploy command
    deploy_parser = subparsers.add_parser("deploy", help="Deploy a challenge")
    deploy_parser.add_argument(
        "--challenge", "-c",
        required=True,
        help="Challenge to deploy (use 'challenges' command to see available)"
    )
    deploy_parser.add_argument("--name", "-n", help="Custom instance name")
    deploy_parser.add_argument("--zone", "-z", default=DEFAULT_ZONE)
    deploy_parser.add_argument("--machine-type", "-m", default=DEFAULT_MACHINE_TYPE)

    # List running instances command
    subparsers.add_parser("list", help="List running challenge instances on GCP")

    # List available challenges command
    subparsers.add_parser("challenges", help="List available challenges to deploy")

    # Destroy command
    destroy_parser = subparsers.add_parser("destroy", help="Destroy a challenge")
    destroy_parser.add_argument("--name", "-n", required=True, help="Instance name")
    destroy_parser.add_argument("--zone", "-z", default=DEFAULT_ZONE)

    args = parser.parse_args()

    # Verify gcloud is available
    try:
        get_project_id()
    except FileNotFoundError:
        print("Error: gcloud CLI not found. Install Google Cloud SDK first.")
        sys.exit(1)

    if args.command == "deploy":
        deploy_challenge(
            challenge=args.challenge,
            name=args.name,
            zone=args.zone,
            machine_type=args.machine_type,
        )
    elif args.command == "list":
        list_challenges()
    elif args.command == "challenges":
        print("\n=== Available Challenges ===")
        print(f"(Loaded from {METADATA_CSV})\n")
        if not CHALLENGES and not LOCAL_CHALLENGES:
            print("No challenges found. Check that CSV files exist.")
        else:
            print(f"{'Name':<25} {'Port':<8} {'Image'}")
            print("-" * 70)
            for name, (image, port) in sorted(CHALLENGES.items()):
                print(f"{name:<25} {port:<8} {image}")
            # Show local challenges
            if LOCAL_CHALLENGES:
                print("\n--- Local Dockerfile Challenges (built on deploy) ---")
                for name, (dockerfile_dir, port) in sorted(LOCAL_CHALLENGES.items()):
                    print(f"{name:<25} {port:<8} {dockerfile_dir}")
        print()
    elif args.command == "destroy":
        destroy_challenge(name=args.name, zone=args.zone)


if __name__ == "__main__":
    main()

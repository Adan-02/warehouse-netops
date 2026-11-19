"""Prepare NoCloud ISOs and VMware configs for the three Linux roles."""
import pathlib
import subprocess
import sys
from virtualization.runtime import MACS, PRIVATE, PROJECT, tool, vmx, ensure_stopped, load_paths


def main(argv=None):
    from virtualization.bootstrap.guards import parse_bootstrap, require_staging
    parse_bootstrap(__doc__, argv)
    require_staging()

    # All roles are checked before writing even one seed or ISO.
    for role in ("service", "iot", "office"):
        target = ensure_stopped(role)
        if target.exists() or (target.parent / 'seed').exists() or (target.parent / 'seed.iso').exists():
            raise FileExistsError(f'{role}: existing prepared VM protected; not overwriting seed/ISO/VMX')
    for role in ("service", "iot", "office"):
        d = load_paths().vms / role
        if not (d / f"{role}.vmdk").exists():
            raise FileNotFoundError(f"Prepare {role} disk first")
        subprocess.run([sys.executable, str(PROJECT / "virtualization/debian/generate_seed.py"),
                        "--role", role, "--output-dir", str(d / "seed"),
                        "--ssh-public-key-file", str(PRIVATE / "lab_ed25519.pub"),
                        "--instance-id", f"warehouse-{role}-v1", "--mac", MACS[role][0]], check=True)
        prefix = f"/work/vms/{role}"
        tool("genisoimage", "-output", prefix + "/seed.iso", "-volid", "cidata", "-joliet", "-rock",
             prefix + "/seed/user-data", prefix + "/seed/meta-data", prefix + "/seed/network-config")
        print(vmx(role, d / f"{role}.vmdk", seed=d / "seed.iso"), flush=True)


if __name__ == "__main__":
    main()

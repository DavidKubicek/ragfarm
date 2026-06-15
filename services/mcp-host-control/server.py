"""
mcp-host-control — GUARDED host operations (reboot) via OpenNebula one.host.*.
SAFETY: dry-run is the DEFAULT. A real action requires confirm=True AND the host
to be in the allowlist. This service can bounce production hosts — treat as such.
STATUS: stub. Implement one.host.action / migration-drain before enabling.
"""
import os
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("host-control", host="0.0.0.0", port=8102)
ALLOWLIST = set(filter(None, os.environ.get("HOST_ALLOWLIST", "").split(",")))

@mcp.tool()
def reboot_host(hostname: str, confirm: bool = False, dry_run: bool = True) -> dict:
    """Reboot a hypervisor host. Defaults to dry-run. Requires confirm=True and
    hostname in HOST_ALLOWLIST to actually act. TODO: drain/migrate VMs first via
    OpenNebula, then issue reboot through the host management path."""
    if hostname not in ALLOWLIST:
        return {"ok": False, "reason": f"{hostname} not in allowlist", "acted": False}
    if dry_run or not confirm:
        return {"ok": True, "acted": False, "would_reboot": hostname,
                "note": "dry-run/no-confirm; set dry_run=False and confirm=True to act"}
    # TODO: real implementation (drain -> reboot). Intentionally not wired yet.
    return {"ok": False, "acted": False, "reason": "not implemented — safety gate"}

if __name__ == "__main__":
    mcp.run(transport="streamable-http")

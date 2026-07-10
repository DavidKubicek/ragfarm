"""
mcp-host-control — GUARDED host operations (drain + reboot) via OpenNebula.
SAFETY (unchanged): dry-run is the DEFAULT. A real action requires confirm=True
AND the host in HOST_ALLOWLIST. This service can bounce production hosts.

Two-phase contract the OWUI confirmation wrapper relies on:
  reboot_host(host, confirm=False) -> a PLAN (what it would do), acts on nothing.
  reboot_host(host, confirm=True)  -> performs drain-then-reboot (allowlisted only).

HOST_MOCK=1 -> simulate drain/reboot against canned VM->host data (no OpenNebula),
so the integration and the human-in-the-loop confirmation UX can be tested with no
cluster. Clearly a test mock; outputs carry "mock": true and nothing real happens.
Real OpenNebula drain (one.vm.migrate live) + reboot stay TODO behind the gate.
"""
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("host-control", host="0.0.0.0", port=8102)
ALLOWLIST = set(filter(None, os.environ.get("HOST_ALLOWLIST", "").split(",")))
HOST_MOCK = os.environ.get("HOST_MOCK", "").lower() in ("1", "true", "yes")

# canned VMs per host (consistent with mcp-infra-placement's mock fixture)
_MOCK_HOST_VMS = {
    "node-01": [{"vm_id": 101, "name": "web-prod-1"}, {"vm_id": 103, "name": "db-prod-1"}],
    "node-02": [{"vm_id": 102, "name": "web-prod-2"}, {"vm_id": 105, "name": "cache-1"}],
    "node-03": [{"vm_id": 104, "name": "sftp-gw"}],
}


def _plan(hostname: str) -> dict:
    vms = _MOCK_HOST_VMS.get(hostname, []) if HOST_MOCK else []
    names = ", ".join(v["name"] for v in vms) or "(none)"
    return {
        "hostname": hostname,
        "vms_to_drain": vms,
        "steps": [
            f"live-migrate {len(vms)} VM(s) off {hostname}: {names}",
            f"wait for {hostname} to be empty",
            f"reboot {hostname}",
            f"wait for {hostname} to rejoin the cluster",
        ],
        "summary": f"Drain {len(vms)} VM(s) from {hostname} then reboot it.",
    }


@mcp.tool()
def reboot_host(hostname: str, confirm: bool = False) -> dict:
    """Drain a hypervisor host's VMs then reboot it. TWO-PHASE + SAFETY-GATED.

    Call with confirm=False (default) to get the PLAN only — nothing is acted on.
    Call with confirm=True to actually drain-then-reboot; requires the host to be in
    the allowlist. Intended to be driven behind a human confirmation step.
    """
    plan = _plan(hostname)
    if not confirm:
        return {"ok": True, "acted": False, "phase": "plan", "plan": plan,
                "allowlisted": hostname in ALLOWLIST, "mock": HOST_MOCK,
                "note": "dry-run: call again with confirm=True to act"}
    if hostname not in ALLOWLIST:
        return {"ok": False, "acted": False, "reason": f"{hostname} not in allowlist"}
    if HOST_MOCK:
        return {"ok": True, "acted": True, "phase": "executed", "mock": True,
                "drained": plan["vms_to_drain"], "rebooted": hostname,
                "note": "SIMULATED — no real drain/reboot performed"}
    # TODO: real drain (one.vm.migrate live) then reboot via host management path.
    return {"ok": False, "acted": False, "reason": "real drain/reboot not implemented — safety gate"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

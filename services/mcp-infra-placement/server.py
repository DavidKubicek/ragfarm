"""
mcp-infra-placement — MCP server answering "where is VM<n> running?" against
OpenNebula via its XML-RPC API (one.vm.info / one.vmpool.info).

Transport: streamable HTTP (MCP), so it registers cleanly with the LLM/agent
gateway as an HTTP microservice. Read-only: it never mutates infra (host reboot
lives in the separate, guarded mcp-host-control service).

OpenNebula mapping:
  - VM -> running host is in the VM's <HISTORY_RECORDS>/<HISTORY>/<HOSTNAME> (last record),
    and VM <STATE>/<LCM_STATE> give lifecycle status.
  - We resolve a VM by numeric id (one.vm.info) or by name (scan one.vmpool.info).
"""
from __future__ import annotations

import os
import xmlrpc.client
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

ONE_XMLRPC = os.environ.get("ONE_XMLRPC", "http://opennebula.infra.local:2633/RPC2")
# OpenNebula auth is "user:password" (or user:token). Keep it in .env / secret store.
ONE_AUTH = os.environ.get("ONE_AUTH", "oneadmin:CHANGEME")

mcp = FastMCP("infra-placement", host="0.0.0.0", port=8101)
_proxy = xmlrpc.client.ServerProxy(ONE_XMLRPC, allow_none=True)


# --- OpenNebula state code -> human label (subset that matters operationally) ---
_LCM = {
    0: "LCM_INIT", 3: "RUNNING", 4: "MIGRATE", 5: "SAVE_STOP", 36: "HOTPLUG",
    # ... extend as needed; full table in OpenNebula docs.
}
_VM_STATE = {
    0: "INIT", 1: "PENDING", 2: "HOLD", 3: "ACTIVE", 4: "STOPPED",
    5: "SUSPENDED", 6: "DONE", 8: "POWEROFF", 9: "UNDEPLOYED",
}


@dataclass
class Placement:
    vm_id: int
    name: str
    state: str
    lcm_state: str
    host: str | None
    host_id: str | None
    cluster: str | None


def _one_call(method: str, *params):
    """Call an OpenNebula XML-RPC method; first arg is always the auth string.
    OpenNebula returns [success(bool), payload(str|int), errcode(int)]."""
    ok, payload, *_ = getattr(_proxy, method)(ONE_AUTH, *params)
    if not ok:
        raise RuntimeError(f"OpenNebula {method} failed: {payload}")
    return payload


def _parse_vm(xml_str: str) -> Placement:
    root = ET.fromstring(xml_str)
    vm_id = int(root.findtext("ID", default="-1"))
    name = root.findtext("NAME", default="")
    state = _VM_STATE.get(int(root.findtext("STATE", "0")), "UNKNOWN")
    lcm = _LCM.get(int(root.findtext("LCM_STATE", "0")), root.findtext("LCM_STATE", "0"))
    # The current host is the last HISTORY record.
    host, host_id = None, None
    histories = root.findall("./HISTORY_RECORDS/HISTORY")
    if histories:
        last = histories[-1]
        host = last.findtext("HOSTNAME")
        host_id = last.findtext("HID")
    cluster = root.findtext("./HISTORY_RECORDS/HISTORY[last()]/CID")
    return Placement(vm_id, name, state, lcm, host, host_id, cluster)


def _find_vm_id_by_name(name: str) -> int:
    # one.vmpool.info(filter=-2 all, -1, -1, state=-1) -> XML of all VMs.
    xml_str = _one_call("one.vmpool.info", -2, -1, -1, -1)
    root = ET.fromstring(xml_str)
    matches = [int(vm.findtext("ID")) for vm in root.findall("VM")
               if vm.findtext("NAME") == name]
    if not matches:
        raise LookupError(f"No VM named {name!r}")
    if len(matches) > 1:
        raise LookupError(f"Multiple VMs named {name!r}: ids {matches} — query by id")
    return matches[0]


@mcp.tool()
def where_is_vm(vm: str) -> dict:
    """Return where a VM is running. `vm` may be a numeric OpenNebula VM id or a VM name.

    Returns the VM's name, lifecycle state, and the host (hypervisor) it is
    currently placed on according to OpenNebula's scheduler/history.
    """
    try:
        vm_id = int(vm)
    except ValueError:
        vm_id = _find_vm_id_by_name(vm)
    p = _parse_vm(_one_call("one.vm.info", vm_id))
    return {
        "vm_id": p.vm_id,
        "name": p.name,
        "state": p.state,
        "lcm_state": p.lcm_state,
        "host": p.host,
        "host_id": p.host_id,
        "cluster_id": p.cluster,
        "running": p.state == "ACTIVE" and p.lcm_state == "RUNNING",
    }


@mcp.tool()
def list_vms_on_host(hostname: str) -> dict:
    """List VMs currently placed on a given host (hypervisor) by hostname."""
    xml_str = _one_call("one.vmpool.info", -2, -1, -1, 3)  # state 3 = ACTIVE
    root = ET.fromstring(xml_str)
    out = []
    for vm in root.findall("VM"):
        p = _parse_vm(ET.tostring(vm, encoding="unicode"))
        if p.host == hostname:
            out.append({"vm_id": p.vm_id, "name": p.name, "lcm_state": p.lcm_state})
    return {"host": hostname, "count": len(out), "vms": out}


if __name__ == "__main__":
    # Streamable HTTP transport so the agent gateway can register it over HTTP.
    mcp.run(transport="streamable-http")

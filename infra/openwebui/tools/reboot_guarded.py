"""
title: Guarded host reboot
description: Drain-then-reboot a hypervisor, gated by a human confirmation modal.
author: ragfarm
version: 0.1.0
"""
# Open WebUI native Python Tool (ADR-0004 confirmation gate). It is the ONLY path
# by which the model can reach the mutating host-control op: host-control is bridged
# by mcpo but NOT registered as an Open WebUI tool server, so the model cannot call
# reboot directly — it must go through this wrapper, which:
#   1. asks host-control for a dry-run PLAN (confirm=False),
#   2. shows that plan in a blocking confirmation modal (__event_call__),
#   3. only on human approval calls host-control with confirm=True.
# The LLM is out of the confirm loop entirely; the human is the gate. The
# server-side allowlist + confirm gate in mcp-host-control still applies underneath.
import requests

MCPO = "http://127.0.0.1:8000"  # mcpo OpenAPI (host networking); /host-control/*


class Tools:
    def __init__(self):
        pass

    async def reboot_host(self, hostname: str, reason: str = "", __event_call__=None) -> str:
        """
        Drain a hypervisor host's VMs and reboot it. Shows the exact plan and
        REQUIRES explicit human confirmation in the UI before doing anything.
        Use this whenever the user asks to reboot / restart / bounce a hypervisor host.

        :param hostname: the hypervisor host to reboot (e.g. node-03)
        :param reason: short reason for the reboot, recorded for the audit trail
        """
        # 1. dry-run -> plan (no action)
        try:
            plan = requests.post(
                f"{MCPO}/host-control/reboot_host",
                json={"hostname": hostname, "confirm": False}, timeout=30,
            ).json()
        except Exception as e:
            return f"Could not reach host-control: {e}"
        if not plan.get("ok"):
            return f"Cannot plan reboot of {hostname}: {plan.get('reason')}"

        p = plan.get("plan", {})
        vms = p.get("vms_to_drain", [])
        vm_lines = "\n".join(f"  •  {v['name']}  (VM {v['vm_id']})" for v in vms) or "  •  (none)"
        # ---- confirmation dialog text (edit here; no container rebuild needed) ----
        preview = (
            f"You are about to drain and reboot hypervisor {hostname}.\n\n"
            f"Reason:  {reason or '—'}\n\n"
            f"{len(vms)} running VM(s) will be live-migrated to other hosts first:\n"
            f"{vm_lines}\n\n"
            f"Sequence:\n"
            f"  1.  Live-migrate the VMs above off {hostname}\n"
            f"  2.  Wait until {hostname} is empty\n"
            f"  3.  Reboot {hostname}\n"
            f"  4.  Wait for {hostname} to rejoin the cluster\n\n"
            f"Approve to proceed."
        )

        # 2. human confirmation modal — refuse to act without an interactive session
        if __event_call__ is None:
            return "This action requires interactive confirmation and cannot run headlessly."
        approved = await __event_call__(
            {
                "type": "confirmation",
                "data": {"title": f"Reboot hypervisor {hostname}?", "message": preview},
            }
        )
        if not approved:
            return f"Reboot of {hostname} was cancelled. No changes were made."

        # 3. execute (confirm=True); server-side allowlist still gates this
        res = requests.post(
            f"{MCPO}/host-control/reboot_host",
            json={"hostname": hostname, "confirm": True}, timeout=60,
        ).json()
        if not res.get("ok"):
            return f"Reboot could not proceed: {res.get('reason')}."
        drained = res.get("drained", [])
        names = ", ".join(v["name"] for v in drained) or "no"
        return (
            f"{hostname} has been rebooted successfully. {len(drained)} VM(s) "
            f"({names}) were live-migrated beforehand, and {hostname} has rejoined the cluster."
        )

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
        steps = "\n".join(f"  - {s}" for s in p.get("steps", []))
        preview = (
            f"Host: {hostname}\n"
            f"Reason: {reason or '(none given)'}\n"
            f"{p.get('summary', '')}\n"
            f"Allowlisted: {plan.get('allowlisted')}   Mock: {plan.get('mock')}\n"
            f"Steps:\n{steps}"
        )

        # 2. human confirmation modal — refuse to act without an interactive session
        if __event_call__ is None:
            return "No interactive session to confirm in; refusing to act.\n\n" + preview
        approved = await __event_call__(
            {
                "type": "confirmation",
                "data": {"title": f"Reboot {hostname}?", "message": preview},
            }
        )
        if not approved:
            return f"Reboot of {hostname} CANCELLED by user — no action taken.\n\n{preview}"

        # 3. execute (confirm=True); server-side allowlist still gates this
        res = requests.post(
            f"{MCPO}/host-control/reboot_host",
            json={"hostname": hostname, "confirm": True}, timeout=60,
        ).json()
        if not res.get("ok"):
            return f"Reboot refused by host-control: {res.get('reason')}"
        return (
            f"Reboot executed for {hostname}: {res.get('note')}. "
            f"Drained {len(res.get('drained', []))} VM(s)."
        )

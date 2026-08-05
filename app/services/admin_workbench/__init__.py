"""Read models for the Admin Workbench.

The Admin Workbench is the operator-facing control and diagnosis centre. This
package projects the durable Phase 2 domain state — Campaigns, Campaign
Contacts, Agent/Stage states, Agent Jobs, attempts, evidence and audit history
— into presentation view models organised the way an operator reasons:

    Campaign -> Contacts -> Agent/Stage progress -> worker -> Agent Job
    -> attempt -> evidence, output, failure and available corrective action.

Nothing in this package holds authority. Every state value is one the domain
services committed; every mutation offered by the Workbench routes goes through
the existing authoritative write surfaces (``workbench_agents.commands`` and
friends), never through anything defined here.
"""

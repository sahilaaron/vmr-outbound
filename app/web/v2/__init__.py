"""The customer-facing v2 interface.

A second, separate presentation layer over the same services the operator
Workbench uses. It owns its own templates, its own stylesheet and its own router,
and imports nothing from ``app.web.routes`` — so the admin surface stays exactly
as it was and neither can break the other by accident.
"""

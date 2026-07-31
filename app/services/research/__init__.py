"""Company research (RES-001): workers, the dossier step machine, and the agent.

This file exists to make ``app.services.research`` a *regular* package, and that
is not a formality.

Without it setuptools classifies the directory as a namespace package, and an
editable install records it as one::

    NAMESPACES = {'app.services.research': ['/the/checkout/where/pip/ran/...']}

A namespace package has no single home: its ``__path__`` is assembled from every
matching directory found on ``sys.path``, in path order. With two checkouts of
this repository visible to one interpreter — a git worktree sharing a ``.venv``
with the original checkout, which is how a UAT branch is normally run beside main
— ``app.services.research`` could resolve to the other tree, or worse, splice
directories from both. Submodules then load or fail to load depending on which
checkout happened to be first, which is how the Research Agent came to fail every
job with a bare ``ModuleNotFoundError`` while the same branch's web application
ran perfectly: the two entry points put different directories on the path.

Every other Python package under ``app/`` already has one of these; only this
directory did not, so only this subtree could be assembled from two sources.
Regular packages are unambiguous — one ``__init__.py``, one home, one answer.
"""

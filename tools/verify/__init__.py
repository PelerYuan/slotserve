"""L4 verification of a running SlotServe on real hardware (A11): ``python -m tools.verify --help``.

Talks to the server only through its public API (and SSH into dev environments), never imports SlotServe, and
needs only ``httpx`` (plus ``ssh``, ``ssh-keygen`` and ``sshpass`` for the login checks). The Markdown/JSON report
it writes goes with the release PR (R1-17). Checks that need a feature the server does not have yet are reported
as *pending*; checks a person has to do in a browser as *manual*, with instructions.
"""

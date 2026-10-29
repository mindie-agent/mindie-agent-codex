"""The fixed, noneditable Codex adapter package.

[Role]
Declares the installed Codex adapter package.

[Invariants]
Importing the package does not activate knowledge or dispatch host tools.

[Timeouts & Locks]
No blocking operations or locks during package import.

[Side-Effect Policy]
No filesystem, service, or remote mutations.
"""

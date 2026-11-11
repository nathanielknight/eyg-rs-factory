This is the factory's coordinator: tickets, dev runs, validation runs and merges. It runs locally. Nat manages tickets, notes and reviews in the Django admin; batch commands (`dispatch`, `collect`, `validate`, `merge`, `sweep_runs`) start development and test runs in Incus VMs and merge what passes.

Nothing in a VM talks to the factory. A dev run is a function call: the ticket and its notes go in as files, the agent works, and its notes, an optional submission and a bundle of its commits come out as files (see `workflow/dev.py`).

Git changes move as bundles to minimize surface area.

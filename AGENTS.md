This is the coordination and oracle repo for an LLM-driven implementation of an [EYG] interpreter--not the interpreter itself, but the infrastructure, oracle, holdout tests, etc. It implements the software "dark factory" pattern.

[EYG]: https://eyg.run/

The components of the system are as follows:

- Virtual machines managed with `incus` to host LLM-driven development and test runs.
- A minimal API that runs on the host server that:
  - coordinates tasks, work, and validation runs
  - accepts chat logs and merge requests
  - merges changes into the git repo
- Tests/oracles based on the existing EGY interpreter



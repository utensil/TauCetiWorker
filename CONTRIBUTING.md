# Contribution policy for the scoped-review fork

`dev` is the production branch. Review installations pin a verified commit from it.

- Upstream maintenance: fetch `kim-em/TauCetiWorker:main`, merge it into `dev`, resolve conflicts
  locally, run the repository gates, then push directly to `dev`. `scripts/sync-upstream --push`
  handles the clean-merge path and stops for manual conflict resolution.
- Fork features: use a feature branch and open a pull request targeting `dev`. A human must approve
  the PR before merge.
- Fork `main`: not managed by the production sync script; the repository owner updates it separately.

No review round should install a moving branch. Pin the verified `dev` commit used for that round.

On macOS, `scripts/sync-upstream` runs the full standalone suite except the two upstream probes that
are Linux-specific in practice (`kiro.py` and `worker_manager.py`). The resulting `dev` commit must
also pass the public Linux GitHub Actions suite before it is eligible for a review round.

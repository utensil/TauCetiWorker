# Contribution policy for the scoped-review fork

`dev` is the production branch. Review installations pin a verified commit from it.

- Upstream maintenance: fetch `kim-em/TauCetiWorker:main`, merge it into `dev`, resolve conflicts
  locally, run the repository gates, then push directly to `dev`. `scripts/sync-upstream --push`
  handles the clean-merge path and stops for manual conflict resolution.
- Fork features: use a feature branch and open a pull request targeting `dev`. A human must approve
  the PR before merge.
- Fork `main`: not managed by the production sync script; the repository owner updates it separately.

No review round should install a moving branch. Pin the verified `dev` commit used for that round.

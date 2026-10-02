# Working on incy-routing

You are probably an AI session that has never seen this repository. Read this first; it is short.
`README.md` explains the system, and `DECISIONS.md` explains why it is the way it is.

## Who decides what

The owner directs; the session builds. Within a task you have a free hand, except that you ask the owner first
before you:

- add a runtime dependency, or a fetch from any new third party;
- rename a profile or move its file. Phones and the server's health check depend on both (`DEPLOY.md`);
- touch `.github/`, which is the deploy gate;
- move the pins in `tools/routing_check.sh` without updating the routing cases that depend on them.

## `master` is production

A green push to `master` is live within minutes: CI tests it, publishes the image, and the server deploys it
behind a health check that rolls itself back. Work in progress goes on a branch.

## This repository is public

Nothing about the owner's server goes in here: no hostname, address or path. That lives in his private
infrastructure repository.

## Done means

- `make check` passes, with no skips.
- `make routing` passes when routing changed.
- The reason for anything non-obvious is in `DECISIONS.md`.

# Maintainer instructions

## Local checks

Run `just check`, `just test`, and `just test-integration`. Tests use fake credentials and a disposable CA, not live Cloudflare or public ACME.

`just build` creates `caddy-cloudflare-proxy:dev` in the active Docker engine and validates Caddy offline. Use that same engine when testing it. Compilation precedes project source copies, so Python/Caddyfile edits reuse the binary. Go and xcaddy are absent from the runtime. All local builds stay native; GitHub handles amd64/arm64.

## Update dependencies

```sh
just update-deps
# Preview without writing:
python3 scripts/update_deps.py --dry-run
```

The updater needs host Python 3 and access to GitHub, Docker Hub, and the Go module proxy. It refreshes stable Caddy/provider versions, official multiarchitecture image digests, GitHub Actions tags, and integration-test version assertions. An optional exported `GITHUB_TOKEN` avoids unauthenticated API rate limits.

Review `git diff` and run the checks before pushing. The updater makes ordinary working-tree writes; inspect or revert partial changes with Git if it fails. It never builds, deploys, commits, pushes, reads `.env`, or changes consumer image settings.

The Dockerfile frontend, Just, and runner labels float within their configured tracks. Transitive Go modules and Alpine packages have no separate locks; the pins do not promise reproducible image bytes.

## Publish a version

Push a new stable version tag when publication is authorized:

```sh
git tag -a v1.0.0 -m 'Release v1.0.0'
git push origin v1.0.0
```

The single GitHub workflow tests pushes and pull requests. Only a `vMAJOR.MINOR.PATCH` tag push can publish, after the tests pass. It builds both architectures, verifies the published image, and creates release notes with the version tag and an optional digest pin. There is no custom release package. Never move released tags or overwrite released image versions.

The repository and GHCR package must both be public for anonymous installation. GitHub makes new packages private by default. After the first publication, set the package visibility to public and verify an anonymous pull. This is a one-time owner setting, not a consumer setup step. Publishing still authenticates with the workflow's `GITHUB_TOKEN`.

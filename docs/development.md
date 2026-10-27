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

Merge the changes into `main`, then open **Actions → Test and release → Run workflow**. Select `main` and enter an unused stable version such as `v1.0.1`. With the GitHub CLI, the equivalent command is:

```sh
gh workflow run ci.yml --ref main -f version=v1.0.1
```

This command starts publication. Do not create or push a version tag yourself. Branch pushes and pull requests run tests only; tag pushes do not trigger the workflow. Manual runs require a `vMAJOR.MINOR.PATCH` version of at most 128 characters, without leading zeros or prerelease suffixes, and refuse branches other than `main`.

The workflow uses the exact commit recorded when the run starts, even if `main` advances:

1. Run local checks, unit tests, integration tests, and multiarchitecture unit tests.
2. Require the version to be absent from Git tags, GitHub releases (including drafts), and GHCR image tags. Authentication, network, and unexpected lookup failures stop publication.
3. Build and push `candidate-<run-id>-<attempt>` for `linux/amd64` and `linux/arm64`. Release runs use this build instead of the normal CI cache-only release build. The jobs share a BuildKit cache.
4. Verify the candidate by digest: require both architectures, run amd64 integration tests, and run an arm64 offline configuration check.
5. Check version availability again, then copy the verified image index to the version tag without rebuilding. Require the version tag to resolve to the same digest, preserving its provenance and SBOM attestations.
6. Create the Git tag at the tested commit, then create the GitHub release with the image reference, optional digest pin, and matching examples.

Only the publication job has write permissions. It uses the workflow's `GITHUB_TOKEN`; no separate publishing secret is needed. Publications run one at a time and do not cancel an active publication. Dispatch one release at a time: GitHub concurrency can replace an older pending job when more jobs queue. No `latest`, major-version, or minor-version image aliases are created.

The availability check is a guard, not a registry lock. Keep this workflow as the only writer of release versions. Protect `main` and restrict who can create release tags or write to the package. Never move released Git tags or overwrite released image versions.

The repository and GHCR package must both be public for anonymous installation. GitHub makes new packages private by default. After the first publication, set the package visibility to public and verify an anonymous pull. This is a one-time owner setting, not a consumer setup step.

### Recover an interrupted publication

GitHub and GHCR cannot update atomically. The run summary records the version, tested commit, candidate reference, and digest before verification. A separate summary entry confirms successful verification. Use that record, not the current `main` commit or a rebuilt image.

- **Before version promotion:** no official version exists. Fix the failure and rerun or dispatch again. Each attempt gets a new candidate tag. Failed verification never creates a versioned image, Git tag, or release.
- **After version promotion:** do not rerun publication blindly. The availability check intentionally refuses an existing version. Check that the versioned image resolves to the exact digest from a successfully verified candidate. If it differs, stop and investigate; do not overwrite it or create a release for it.
- **Image published, Git tag missing:** confirm the Git tag is absent through a successful GitHub lookup, then create only the missing tag at the recorded commit.
- **Git tag exists, GitHub release missing:** confirm the tag points to the recorded commit, then create only the missing release. If both already exist and match the verified image, no publication retry is needed.

For an authorized manual recovery, set these values from the successful candidate verification record:

```sh
VERSION=v1.0.1
SHA='<tested-commit-sha>'
IMAGE=ghcr.io/lingmann/caddy-cloudflare-proxy
DIGEST='sha256:<verified-index-digest>'

# Must print exactly $DIGEST. A missing image or lookup failure is not a match.
docker buildx imagetools inspect "$IMAGE:$VERSION" --format '{{.Manifest.Digest}}'

# Inspect matching Git tags; this lookup must succeed.
gh api "repos/lingmann/caddy-cloudflare-proxy/git/matching-refs/tags/$VERSION"

# Only if the exact tag is absent and the image digest matched:
gh api --method POST repos/lingmann/caddy-cloudflare-proxy/git/refs \
  -f "ref=refs/tags/$VERSION" -f "sha=$SHA"

# Only if the tag points to $SHA and the release is absent:
gh release create "$VERSION" --verify-tag --title "$VERSION" \
  --notes "Verified linux/amd64 and linux/arm64 image: $IMAGE:$VERSION. Optional digest pin: $IMAGE@$DIGEST. Quickstart: https://github.com/lingmann/caddy-cloudflare-proxy/tree/$VERSION#quickstart"
```

These commands do not validate the recovery record for you. If you cannot establish a matching verified digest and commit, leave the partial version untouched and investigate before choosing a new version. Candidate cleanup is not automatic. Do not delete a GHCR package version just to remove a candidate tag: a release tag can point to that same package version.

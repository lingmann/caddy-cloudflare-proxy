set default-list

# Refresh stable Caddy/provider/image and GitHub Actions pins without building or deploying.
update-deps:
    python3 scripts/update_deps.py

# Build a native development image and smoke-test Caddy offline with fake credentials.
build:
    docker build --target release -t caddy-cloudflare-proxy:dev .
    docker run --rm --network none -e PROXY_HOSTNAMES=service.example.com -e PROXY_UPSTREAM=app:8080 -e CLOUDFLARE_API_TOKEN=0000000000000000000000000000000000000000 caddy-cloudflare-proxy:dev check

# Run Python unit tests inside the image, without network access during tests.
test:
    docker build --target test -t caddy-cloudflare-proxy:test .

# Test the built image with isolated containers, fake credentials, and a local test CA.
test-integration: build
    bash tests/integration.sh

# Check operator examples and this justfile without contacting an engine or Cloudflare.
check:
    just --fmt --check
    bash tests/examples.sh

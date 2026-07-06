# GitHub MCP Server
#
# Builds from the public upstream source so you can review the code before
# running it. Source: https://github.com/github/github-mcp-server
#
# The gateway injects each user's GitHub PAT as an Authorization: Bearer header
# via the sidecar's get_connection_headers tool. No GITHUB_PERSONAL_ACCESS_TOKEN
# environment variable is needed in the deployment — the token is per-user and
# injected at request time.
#
# Build:
#   docker build -t <your-registry>/mcp-github:latest .
#
# The build clones the upstream repo at build time. Pin to a specific tag or
# commit SHA (--build-arg TAG=v0.x.x) for reproducible builds.

ARG TAG=latest

FROM golang:1.23-alpine AS builder
RUN apk add --no-cache git ca-certificates
ARG TAG
WORKDIR /build
RUN git clone --depth 1 \
    $([ "$TAG" = "latest" ] && echo "" || echo "--branch $TAG") \
    https://github.com/github/github-mcp-server .
RUN go build -o github-mcp-server ./cmd/github-mcp-server

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
WORKDIR /app
COPY --from=builder /build/github-mcp-server .
EXPOSE 8080
ENTRYPOINT ["/app/github-mcp-server"]
CMD ["http", "--port", "8080"]

# Reference image for Docker Sandbox (`dle_docker_sandbox` in Settings).
#
# This is not built or pushed automatically by anything in this repo — it's
# a starting point you build yourself and point `docker_sandbox_image` at:
#
#   docker build -t supersonic-sandbox:latest -f docker/sandbox.Dockerfile .
#
# Then set docker_sandbox_image=supersonic-sandbox:latest in Settings (or
# via `PUT /api/secrets`) and turn on dle_docker_sandbox.
#
# What's preinstalled here, and why only these three: Claude Code and Codex
# both ship as documented npm packages, and Aider as a documented pip
# package — all three install commands below are the same ones this
# codebase's own doctor/runner logic already assumes are correct
# (agents/runner.py references `@openai/codex` directly; Claude Code CLI is
# `@anthropic-ai/claude-code`; Aider is `aider-chat`). OpenCode's and Cursor
# Agent's exact package/install commands are NOT baked in here — this
# project doesn't have the same first-hand confidence in their current
# install method, and shipping a guessed command in a Dockerfile nobody here
# could build-test (no Docker daemon was available while writing this) is
# exactly the kind of unverified claim this project tries hard to avoid. If
# you use opencode or cursor as your configured agent, extend this file
# yourself with whatever their current official install instructions are.

FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Codex CLI
RUN npm install -g @openai/codex

# Aider (pip-installed; --break-system-packages needed on Debian's
# externally-managed Python 3.11+ install)
RUN pip3 install --no-cache-dir --break-system-packages aider-chat

WORKDIR /workspace

# No ENTRYPOINT/CMD: sandbox_runner.py appends the actual agent command
# (e.g. `claude -p "..." --dangerously-skip-permissions`) to `docker run`
# itself, so this image just needs the binaries on PATH.

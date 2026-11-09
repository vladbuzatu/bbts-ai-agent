FROM python:3.12-slim-bookworm

# System tools + Node.js 22 (Angular CLI, Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential procps ripgrep util-linux iptables ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI: used only by the bot (agents have no GitHub credentials)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Chromium for screenshots and e2e tests. Pinned, so the `playwright` command always
# matches the installed browser. e2e projects pin @playwright/test to the same version.
ARG PLAYWRIGHT_VERSION=1.65.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN npm install -g playwright@${PLAYWRIGHT_VERSION} --no-audit --no-fund \
    && playwright install --with-deps chromium

# Angular CLI (matches the playbooks); also provides Angular's AI tooling (`ng mcp`)
RUN npm install -g @angular/cli@22 --no-audit --no-fund

# PDF documents (studio): WeasyPrint + fonts with Romanian diacritics + page previews
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu fonts-liberation poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir weasyprint

# Python tooling the backend agent uses
RUN pip install --no-cache-dir ruff pytest pytest-django pip-audit

# Secret scanner for the projects' tests (scripts/verify.sh) and the bot's publish check.
ARG GITLEAKS_VERSION=8.30.1
RUN curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
      | tar -xz -C /usr/local/bin gitleaks && gitleaks version

# CarStage Model Lab (studio agent "model-lab"): its own Python environment for the CarStage
# pipelines, run as the separate user "lab". Build with --build-arg CARSTAGE_LAB=0 to skip (~1 GB).
ARG CARSTAGE_LAB=1
COPY lab-requirements.txt /tmp/lab-requirements.txt
RUN if [ "$CARSTAGE_LAB" = "1" ]; then \
      python -m venv /opt/lab-venv \
      && /opt/lab-venv/bin/pip install --no-cache-dir -r /tmp/lab-requirements.txt \
      && mkdir -p /opt/lab-models \
      && U2NET_HOME=/opt/lab-models /opt/lab-venv/bin/python -c "from rembg import new_session; new_session('u2net')" \
      && chmod -R a+rX /opt/lab-venv /opt/lab-models; \
    fi \
    && useradd -m -u 1001 -d /var/lib/carstage-lab lab \
    && mkdir -p /var/lib/carstage-lab/runs /var/lib/carstage-lab/cache /lab \
    && chown -R lab:lab /var/lib/carstage-lab && chmod 700 /var/lib/carstage-lab
ENV U2NET_HOME=/opt/lab-models

# The bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot/
COPY tests/ ./tests/
COPY workspace-template/ ./workspace-template/
COPY studio-template/ ./studio-template/
COPY master-template/ ./master-template/
RUN ln -sf /app/bot/tools/html2pdf /usr/local/bin/html2pdf && chmod +x /app/bot/tools/html2pdf

# Identities:
#  - root (inside the container) runs the bot and alone holds the Telegram/GitHub/provider keys
#  - "agent" runs the build team: every agent, test, preview and git command on the projects
#    (with AGENT_EGRESS=restricted only this user's internet goes through the allowlist)
#  - "studio" runs studio jobs (website research needs the open web)
#  - "lab" runs CarStage pipelines with the lab's provider keys (created above)
RUN useradd -m -u 1000 agent \
    && useradd -m -u 1002 studio \
    && mkdir -p /workspace /studio /home/agent/.npm-global /home/agent/.claude /data \
             /home/studio/.npm-global /home/studio/.claude \
    && chown -R agent:agent /workspace /home/agent /ms-playwright \
    && chown -R studio:studio /studio /home/studio \
    && chmod a+rX /ms-playwright \
    && chown root:root /data && chmod 700 /data \
    && chmod 755 /app && chmod -R a+rX /app

ENV PATH=/home/agent/.npm-global/bin:/home/agent/.local/bin:$PATH \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    NG_CLI_ANALYTICS=false \
    WORKSPACE_DIR=/workspace \
    DATA_DIR=/data \
    STUDIO_DIR=/studio \
    TEMPLATE_DIR=/app/workspace-template \
    AGENT_USER=agent

RUN for u in root agent studio; do \
      su "$u" -s /bin/sh -c 'git config --global user.name "Agent Team" \
        && git config --global user.email "agents@localhost" \
        && git config --global init.defaultBranch main \
        && git config --global --add safe.directory "*"'; \
    done

WORKDIR /app/bot
CMD ["python", "bot.py"]

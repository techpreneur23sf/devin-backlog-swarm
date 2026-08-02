FROM python:3.12-slim

# The logic lives in the container, not in the workflow YAML: the same image
# runs on a laptop, in GitHub Actions, in GitLab CI and in Jenkins.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY policy.yaml ./policy.yaml
COPY fixtures ./fixtures

ENTRYPOINT ["swarm"]
CMD ["--help"]

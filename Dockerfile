# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM node:22-slim AS frontend
WORKDIR /frontend
COPY ./frontend/package.json ./frontend/package-lock.json ./
RUN npm ci
COPY ./frontend ./
# Served by FastAPI under /ui/; API calls are same-origin (no /api prefix).
RUN VITE_BASE=/ui/ VITE_API_BASE= npx vite build

FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./

COPY ./pm_agent ./pm_agent
COPY ./amos_data ./amos_data
COPY --from=frontend /frontend/dist ./frontend_dist

RUN uv sync --frozen

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "pm_agent.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]

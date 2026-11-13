# NimbusOps Agent

NimbusOps Agent is a Python backend plus React WebUI project.

## Runtime

- Python 3.11 or 3.12
- Node.js with npm, for `webui`

Do not package local dependency directories such as `.venv`, `.AIOPS`, `webui/node_modules`, or `webui/dist`. Rebuild them after extracting the source package.

## Backend Setup

From the project root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

For development and tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Optional extras:

```bash
python -m pip install -e ".[azure]"
python -m pip install -e ".[pdf]"
python -m pip install -e ".[prompt-guardrails]"
```

## WebUI Setup

From the WebUI directory:

```bash
cd webui
npm ci
npm run build
```

The production build writes to `webui/dist`. The directory is generated output and is intentionally not included in the source archive.

## Run

From the project root with the Python virtual environment activated:

```bash
nimbusops init-config
nimbusops serve
```

Then open:

```text
http://127.0.0.1:18790
```

For WebUI development, run the backend and the Vite dev server separately:

```bash
nimbusops serve
```

```bash
cd webui
npm run dev
```

Then open:

```text
http://127.0.0.1:5173
```


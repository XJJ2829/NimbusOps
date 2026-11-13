# NimbusOps WebUI

This directory contains the React WebUI for NimbusOps Agent. The UI structure, components, settings surface, chat layout, streaming display, tool activity view, skills view, and session sidebar come from the original WebUI; the gateway URLs and build output have been adapted for `my_agent`.

## Layout

```text
webui/       Vite + React + TypeScript source tree
webui/dist/  production build served by python -m my_agent serve
```

## Develop

Start the backend from the project root:

```bash
cd /path/to/NimbusOps/nimbusops-agent
python -m my_agent serve
```

Start the WebUI dev server in another terminal:

```bash
cd /path/to/NimbusOps/nimbusops-agent/webui
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

The dev server proxies `/api`, `/webui`, and `/auth` to:

```text
http://127.0.0.1:18790
```

Use `MY_AGENT_API_URL` when the backend listens elsewhere:

```bash
MY_AGENT_API_URL=http://127.0.0.1:19000 npm run dev
```

## Production Build

```bash
cd /path/to/NimbusOps/nimbusops-agent/webui
npm install
npm run build
```

The build writes to `webui/dist`. Restart the backend after building:

```bash
cd /path/to/NimbusOps/nimbusops-agent
python -m my_agent serve
```

Then open:

```text
http://127.0.0.1:18790
```

## Test

```bash
cd /path/to/NimbusOps/nimbusops-agent/webui
npm test
```

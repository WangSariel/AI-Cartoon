# AI-Cartoon Frontend

React + TypeScript + Vite frontend for AI-Cartoon.

## Development

```powershell
npm install
npm.cmd run dev -- --host 127.0.0.1
```

The frontend keeps the existing backend API contract unchanged and proxies `/api` and `/static` to the FastAPI service in development.

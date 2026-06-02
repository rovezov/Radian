# Ollama + Docker Runbook (Windows PowerShell)

This runbook covers how to:
- Start Ollama
- Start Radian Docker services
- Verify everything is running
- Shut down containers and Ollama cleanly

## Prerequisites

- Docker Desktop is installed and running.
- Ollama is installed (default path: `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`).
- You are in the Radian repo folder:

```powershell
Set-Location "C:\Users\rovez\Documents\Personal\Coding\Radian"
```

## Bring Everything Up

### 1) Start Ollama

If Ollama app/service is not already running, start it:

```powershell
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
```

Optional check:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" list
```

### 2) Start Docker services

From the repo root:

```powershell
docker compose up -d
```

This starts:
- `radian` on `http://localhost:7000`
- `chromadb` on `http://localhost:8100`
- `searxng` on `http://localhost:8080`
- `ntfy` on `http://localhost:8091`

### 3) Verify services are healthy

```powershell
docker compose ps
```

Optional quick checks:

```powershell
curl http://localhost:7000
curl http://localhost:8080
```

If `curl` alias behaves differently in your shell, use:

```powershell
Invoke-WebRequest http://localhost:7000 -UseBasicParsing
Invoke-WebRequest http://localhost:8080 -UseBasicParsing
```

## Bring Everything Down

### 1) Stop Docker services

```powershell
docker compose down
```

If you also want to remove volumes (this deletes persisted service data for compose volumes):

```powershell
docker compose down -v
```

### 2) Stop Ollama

If Ollama is running as a process, stop it:

```powershell
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
```

## Common One-Liners

### Start all

```powershell
Set-Location "C:\Users\rovez\Documents\Personal\Coding\Radian"; Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"; docker compose up -d
```

### Stop all

```powershell
Set-Location "C:\Users\rovez\Documents\Personal\Coding\Radian"; docker compose down; Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
```

## Logs / Troubleshooting

View compose logs:

```powershell
docker compose logs -f
```

View only Radian logs:

```powershell
docker compose logs -f radian
```

If containers fail after script edits on Windows, ensure shell scripts in `docker/` use LF line endings (not CRLF).

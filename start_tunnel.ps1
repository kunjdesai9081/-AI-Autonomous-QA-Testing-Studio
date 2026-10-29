# Start Cloudflare Tunnel with HTTP/2 protocol to prevent Mac/Web loading hangs
Write-Host "Stopping any existing cloudflared instances..." -ForegroundColor Cyan
Stop-Process -Name "cloudflared" -Force -ErrorAction SilentlyContinue

$cloudflaredPath = "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflaredPath)) {
    Write-Host "cloudflared.exe not found in $cloudflaredPath." -ForegroundColor Red
    exit 1
}

Write-Host "Launching Cloudflare Tunnel with --edge-ip-version 4 --protocol http2 --url http://127.0.0.1:8501..." -ForegroundColor Green
& $cloudflaredPath tunnel --edge-ip-version 4 --protocol http2 --retries 10 --url http://127.0.0.1:8501

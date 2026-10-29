@echo off
echo ========================================================
echo  Starting Cloudflare Tunnel for QA Studio (HTTP/2 Mode)
echo ========================================================
echo Stopping any existing cloudflared processes...
taskkill /F /IM cloudflared.exe 2>nul
timeout /t 1 /nobreak >nul

echo Starting tunnel with HTTP/2 and IPv4 mode to prevent DNS and loading hangs...
"%LOCALAPPDATA%\cloudflared\cloudflared.exe" tunnel --edge-ip-version 4 --protocol http2 --retries 10 --url http://127.0.0.1:8501
pause

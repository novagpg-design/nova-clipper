# Nova Auto-Clipper

Descarga vídeos de YouTube, detecta los mejores momentos con IA y genera clips MP4 listos para YouTube Shorts.

## Arquitectura

- **auto-clipper.py** — script principal: descarga con yt-dlp, transcripción, análisis con Claude, corte con ffmpeg
- **clipper_service.py** — API FastAPI en :8999, recibe POST /clip de n8n  
- **clipper-service.service** — systemd unit para el servicio HTTP

## Configuración VPS

```bash
cp auto-clipper.py /usr/local/bin/auto-clipper
chmod +x /usr/local/bin/auto-clipper
cp clipper_service.py /opt/nova/clipper_service.py
cp clipper-service.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now clipper-service
```

## Anti-bot YouTube

El clipper usa `--impersonate chrome` + `visitor_data` extraído de una sesión real de Chrome:

```bash
# Refrescar el token (desde DevTools Console en youtube.com):
# copy(window.ytcfg?.data_?.VISITOR_DATA)
# Luego:
echo 'TOKEN' > /opt/nova/yt_visitor_data.txt
```

## n8n

Workflow activo: ID `20654692aa394280`
Trigger: POST al webhook de n8n con { url, max_clips }
El nodo "Llamar VPS auto-clipper" envía a http://172.18.0.1:8999/clip

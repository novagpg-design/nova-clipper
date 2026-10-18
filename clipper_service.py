#!/usr/bin/env python3
"""
Servicio HTTP wrapper para auto-clipper.
Despliega en el VPS como servicio systemd:

1. Copiar a /opt/nova/clipper_service.py
2. pip3 install fastapi uvicorn
3. Crear /etc/systemd/system/clipper-service.service (ver abajo)
4. systemctl enable --now clipper-service

--- /etc/systemd/system/clipper-service.service ---
[Unit]
Description=Nova Auto-Clipper HTTP Service
After=network.target

[Service]
User=root
ExecStart=/usr/bin/python3 /opt/nova/clipper_service.py
Restart=always
RestartSec=5
Environment=PORT=8999

[Install]
WantedBy=multi-user.target
---
"""

import asyncio
import subprocess
import os
import json
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Nova Auto-Clipper Service")


class ClipRequest(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    url: str
    max_clips: str = "3"
    out_dir: str | None = None

    @property
    def max_clips_int(self) -> int:
        try: return int(self.max_clips)
        except: return 3


@app.get("/health")
def health():
    clipper = Path("/usr/local/bin/auto-clipper")
    return {
        "ok": True,
        "clipper_exists": clipper.exists(),
        "clipper_executable": os.access(clipper, os.X_OK),
    }


@app.post("/clip")
async def clip(req: ClipRequest):
    if not req.url.startswith(("https://www.youtube.com", "https://youtu.be")):
        raise HTTPException(400, "Solo URLs de YouTube")

    out_dir = req.out_dir or f"/tmp/clips-{int(time.time())}"

    cmd = [
        "/usr/local/bin/auto-clipper",
        req.url,
        "--max-clips", str(req.max_clips_int),
        "--out-dir", out_dir,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=280)
        output = stdout.decode("utf-8", errors="replace")
        exit_code = proc.returncode
    except asyncio.TimeoutError:
        raise HTTPException(504, "auto-clipper tardó más de 280s")
    except Exception as e:
        raise HTTPException(500, f"Error lanzando auto-clipper: {e}")

    # Leer report.json si existe
    report = {}
    report_path = Path(out_dir) / "report.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
        except Exception:
            pass

    # Extraer clips del output
    import re
    clip_lines = re.findall(r'\[OK\] (clip-\d+\.mp4) \((\d+)s\) — (.+)', output)
    clips = [{"file": f"{out_dir}/{c[0]}", "duration": int(c[1]), "title": c[2]} for c in clip_lines]

    return {
        "exitCode": exit_code,
        "workDir": out_dir,
        "clips": clips,
        "output": output[-3000:],
        "report": report,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8999))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

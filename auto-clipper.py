#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto-clipper — descarga un vídeo largo, extrae transcript, usa Claude para detectar
los 3 mejores momentos clip-worthy y corta los clips con ffmpeg listos para subir como Shorts.

Uso:
  auto-clipper <youtube_url_o_path> [--out-dir DIR] [--max-clips N] [--dry-run]

Salida: clips en /tmp/clips-<id>/ o --out-dir, + informe JSON con timestamps y títulos sugeridos.
"""
import argparse, json, os, re, subprocess, sys, tempfile, urllib.request
from pathlib import Path

UA = 'Mozilla/5.0 (elm-auto-clipper; +https://novagpg.com)'
MAX_CLIP_DURATION = 58  # YouTube Shorts: máx 60s, dejamos margen


def env():
    d = {}
    for path in ['/root/.hermes/.env', os.path.expanduser('~/.config/watch/.env')]:
        try:
            for l in open(path):
                if '=' in l and not l.startswith('#'):
                    k, v = l.split('=', 1)
                    d[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return d


def run(cmd, **kw):
    kw.setdefault('capture_output', True)
    kw.setdefault('text', True)
    kw.setdefault('timeout', 300)
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0 and kw.get('check'):
        raise RuntimeError(r.stderr or r.stdout)
    return r


def download_video(url, out_dir):
    """Descarga vídeo con yt-dlp, devuelve path al archivo."""
    print(f'[clipper] descargando: {url}')
    import os as _os
    COOKIES = ['--cookies', '/opt/nova/yt_cookies.txt'] if _os.path.exists('/opt/nova/yt_cookies.txt') else []
    VISITOR_FILE = '/opt/nova/yt_visitor_data.txt'
    VISITOR = []
    if _os.path.exists(VISITOR_FILE):
        vd = open(VISITOR_FILE).read().strip()
        if vd:
            VISITOR = ['--extractor-args', f'youtube:visitor_data={vd}']
    r = run(['yt-dlp', '--no-warnings', '--impersonate', 'chrome'] + COOKIES + VISITOR + [
             '-o', str(out_dir / 'video.%(ext)s'),
             '--merge-output-format', 'mp4',
             '-f', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
             '--write-info-json', url])
    if r.returncode != 0:
        raise RuntimeError(f'yt-dlp falló: {r.stderr[:500]}')
    videos = list(out_dir.glob('video.mp4')) + list(out_dir.glob('video.mkv')) + list(out_dir.glob('video.webm'))
    if not videos:
        raise RuntimeError('No se encontró archivo de vídeo tras descarga')
    return videos[0]


def get_transcript(url, work_dir):
    """Obtiene transcript con el skill /watch (transcription mode)."""
    watch_script = Path('/root/.hermes/skills') / 'claude-video' / 'watch' / 'scripts' / 'watch.py'
    if not watch_script.exists():
        # Buscar en paths alternativos
        for base in [Path('/usr/local/lib'), Path('/opt'), Path.home() / '.claude' / 'plugins']:
            candidates = list(base.glob('**/watch/scripts/watch.py'))
            if candidates:
                watch_script = candidates[0]
                break
    if watch_script.exists():
        print(f'[clipper] obteniendo transcript con watch.py...')
        r = run(['python3', str(watch_script), url, '--detail', 'transcript', '--no-whisper',
                 '--out-dir', str(work_dir)], timeout=120)
        # Extraer transcript del output
        lines = r.stdout.split('\n')
        in_transcript = False
        segments = []
        for line in lines:
            if line.strip().startswith('```') and in_transcript:
                break
            if in_transcript and line.strip().startswith('['):
                m = re.match(r'\[(\d+:\d+)\]\s*(.*)', line.strip())
                if m:
                    segments.append({'time': m.group(1), 'text': m.group(2)})
            if '## Transcript' in line or '_Source: captions_' in line:
                in_transcript = True
        if segments:
            return segments
    # Fallback: solo yt-dlp captions
    print('[clipper] watch.py no disponible, usando yt-dlp captions...')
    r = run(['yt-dlp', '--no-warnings', '--write-subs', '--write-auto-subs',
             '--skip-download', '--sub-format', 'vtt', '--sub-lang', 'es,en',
             '-o', str(work_dir / 'caps'), url], timeout=60)
    vtt_files = list(work_dir.glob('caps.*.vtt'))
    if not vtt_files:
        return []
    segments = []
    for line in open(vtt_files[0]):
        m = re.match(r'(\d+:\d+:\d+\.\d+) --> (\d+:\d+:\d+\.\d+)', line)
        if m:
            pass  # simplificado
    return segments


def time_to_sec(t):
    """MM:SS o HH:MM:SS a segundos."""
    parts = t.split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return float(t)


def probe_duration(video_path):
    """Duración del vídeo en segundos."""
    r = run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(video_path)])
    d = json.loads(r.stdout)
    return float(d['format'].get('duration', 0))


def ask_claude(transcript_text, title, duration_s, max_clips, E):
    """Usa Claude API para identificar los mejores momentos clip-worthy."""
    api_key = E.get('ANTHROPIC_API_KEY')
    if not api_key:
        print('[clipper] Sin ANTHROPIC_API_KEY, usando heurística de energía...')
        return None

    prompt = f"""Eres un experto en clipping de contenido para YouTube Shorts y TikTok.

Vídeo: "{title}" (duración: {int(duration_s)}s)

TRANSCRIPT:
{transcript_text[:4000]}

Identifica exactamente {max_clips} momentos que serían mejores clips autónomos de 30-58 segundos para YouTube Shorts.
Criterios: gancho potente al inicio, valor standalone sin contexto previo, momento de sorpresa/consejo/resultado.

Responde SOLO con este JSON:
{{
  "clips": [
    {{
      "start_sec": 30,
      "end_sec": 75,
      "titulo_sugerido": "Título gancho para el Short",
      "razon": "por qué este momento funciona como clip"
    }}
  ]
}}"""

    body = json.dumps({
        'model': 'claude-haiku-4-5',
        'max_tokens': 800,
        'messages': [{'role': 'user', 'content': prompt}]
    }).encode()
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        txt = resp['content'][0]['text']
        m = re.search(r'\{.*\}', txt, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as ex:
        print(f'[clipper] Claude API error: {ex}')
    return None


def heuristic_clips(duration_s, max_clips):
    """Fallback: clips uniformemente distribuidos evitando intro/outro."""
    start_offset = min(30, duration_s * 0.1)
    end_limit = duration_s - min(30, duration_s * 0.05)
    usable = end_limit - start_offset
    clip_dur = min(MAX_CLIP_DURATION, usable / max_clips)
    clips = []
    for i in range(max_clips):
        start = start_offset + i * (usable / max_clips)
        end = min(start + clip_dur, end_limit)
        clips.append({'start_sec': int(start), 'end_sec': int(end),
                      'titulo_sugerido': f'Clip {i+1}', 'razon': 'heurística uniforme'})
    return {'clips': clips}


def cut_clip(video_path, start_s, end_s, out_path):
    """Corta clip con ffmpeg, reencoda para compatibilidad Shorts (9:16 si es posible)."""
    dur = end_s - start_s
    if dur > MAX_CLIP_DURATION:
        end_s = start_s + MAX_CLIP_DURATION
        dur = MAX_CLIP_DURATION
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(start_s),
        '-i', str(video_path),
        '-t', str(dur),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        str(out_path)
    ]
    r = run(cmd, timeout=120)
    return r.returncode == 0


def main():
    p = argparse.ArgumentParser(description='Auto-clipper para YouTube Shorts')
    p.add_argument('source', help='URL de YouTube o ruta local')
    p.add_argument('--out-dir', default=None, help='Directorio de salida')
    p.add_argument('--max-clips', type=int, default=3, help='Número de clips a generar (default: 3)')
    p.add_argument('--dry-run', action='store_true', help='Solo analiza, no corta')
    args = p.parse_args()

    E = env()
    work_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix='clips-'))
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f'[clipper] directorio de trabajo: {work_dir}')

    # Descargar o usar path local
    is_local = os.path.exists(args.source)
    if is_local:
        video_path = Path(args.source)
        title = video_path.stem
        info = {}
    else:
        # Obtener título antes de descargar completo
        r = run(['yt-dlp', '--no-warnings', '--print', '%(title)s', args.source], timeout=30)
        title = r.stdout.strip() or 'video'
        info_r = run(['yt-dlp', '--no-warnings', '--dump-json', args.source], timeout=30)
        try:
            info = json.loads(info_r.stdout)
        except Exception:
            info = {}
        video_path = download_video(args.source, work_dir)

    duration_s = probe_duration(video_path)
    print(f'[clipper] título: {title} | duración: {int(duration_s)}s')

    if duration_s < 90:
        print('[clipper] AVISO: vídeo muy corto (<90s), el clip ES el vídeo completo.')

    # Transcript
    transcript_segs = get_transcript(args.source if not is_local else args.source, work_dir)
    transcript_text = '\n'.join(f"[{s['time']}] {s['text']}" for s in transcript_segs) if transcript_segs else '(sin transcript)'
    print(f'[clipper] transcript: {len(transcript_segs)} segmentos')

    # Detectar momentos con Claude
    print(f'[clipper] analizando con Claude para detectar {args.max_clips} mejores momentos...')
    claude_result = ask_claude(transcript_text, title, duration_s, args.max_clips, E)
    if not claude_result:
        claude_result = heuristic_clips(duration_s, args.max_clips)

    clips_info = claude_result.get('clips', [])[:args.max_clips]
    print(f'[clipper] {len(clips_info)} clips detectados')

    results = []
    for i, clip in enumerate(clips_info, 1):
        start_s = clip.get('start_sec', 0)
        end_s = clip.get('end_sec', start_s + 45)
        # Validar bounds
        start_s = max(0, min(start_s, duration_s - 10))
        end_s = min(end_s, start_s + MAX_CLIP_DURATION, duration_s)
        clip_name = f'clip-{i:02d}.mp4'
        clip_path = work_dir / clip_name
        print(f'[clipper] clip {i}: {int(start_s)}s → {int(end_s)}s | "{clip.get("titulo_sugerido", "")}"')

        if not args.dry_run:
            ok = cut_clip(video_path, start_s, end_s, clip_path)
            size_mb = clip_path.stat().st_size / 1024 / 1024 if ok else 0
            print(f'  → {"OK" if ok else "ERROR"} ({size_mb:.1f} MB)')
        else:
            ok = True
            size_mb = 0
            print('  → dry-run, no cortado')

        results.append({
            'clip': clip_name,
            'path': str(clip_path),
            'start_sec': start_s,
            'end_sec': end_s,
            'duracion_s': end_s - start_s,
            'titulo_sugerido': clip.get('titulo_sugerido', f'Clip {i}'),
            'razon': clip.get('razon', ''),
            'size_mb': round(size_mb, 1),
            'ok': ok,
        })

    # Informe JSON
    report = {
        'source': args.source,
        'title': title,
        'duration_s': duration_s,
        'work_dir': str(work_dir),
        'transcript_segments': len(transcript_segs),
        'clips': results,
        'metodo': 'claude' if claude_result and E.get('ANTHROPIC_API_KEY') else 'heuristico',
    }
    report_path = work_dir / 'report.json'
    json.dump(report, open(report_path, 'w'), ensure_ascii=False, indent=2)
    print(f'\n[clipper] INFORME: {report_path}')
    for r in results:
        status = 'OK' if r['ok'] else 'ERR'
        print(f'  [{status}] {r["clip"]} ({r["duracion_s"]:.0f}s) — {r["titulo_sugerido"]}')
    print(f'[clipper] directorio: {work_dir}')


if __name__ == '__main__':
    main()

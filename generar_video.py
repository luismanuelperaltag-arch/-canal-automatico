"""Canal automatico: noticia -> guion (Groq) -> voz (Edge TTS) -> video (Pixabay + FFmpeg) -> YouTube."""
import asyncio
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIGURACION (se puede cambiar desde el archivo del workflow, sin tocar codigo)
# ----------------------------------------------------------------------------
RAIZ = Path(__file__).resolve().parent
HISTORIAL = RAIZ / "historial.json"

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
MODELOS_GROQ = [m for m in (os.environ.get("GROQ_MODEL"), "llama-3.3-70b-versatile", "llama-3.1-8b-instant") if m]

NOMBRE_CANAL = os.environ.get("NOMBRE_CANAL", "Qué Pasa y Por Qué")
VOZ = os.environ.get("VOZ", "es-MX-JorgeNeural")
VELOCIDAD = os.environ.get("VELOCIDAD", "+0%")
SEGUNDOS = int(os.environ.get("SEGUNDOS_VIDEO", "60"))
PRIVACIDAD = os.environ.get("PRIVACIDAD", "private")
CANTIDAD = int(os.environ.get("CANTIDAD", "1") or "1")

ANCHO, ALTO = 1080, 1920
NUM_ESCENAS = 6
PALABRAS_POR_SEGUNDO = 2.4

# Fuentes de noticias (RSS). Solo se usan como punto de partida: el guion se escribe con palabras nuevas.
FUENTES = [
    ("BBC Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml"),
    ("DW Español", "https://rss.dw.com/rdf/rss-sp-all"),
    ("France 24 Español", "https://www.france24.com/es/rss"),
    ("ONU Noticias", "https://news.un.org/feed/subscribe/es/news/all/rss.xml"),
]

BUSQUEDAS_RESPALDO = ["city skyline", "world map", "technology", "nature landscape", "people walking", "newspaper"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; CanalAutomatico/1.0)"}


# ----------------------------------------------------------------------------
# HISTORIAL (para no repetir noticias ni videos de fondo)
# ----------------------------------------------------------------------------
def cargar_historial():
    if HISTORIAL.exists():
        try:
            datos = json.loads(HISTORIAL.read_text(encoding="utf-8"))
            datos.setdefault("videos", [])
            datos.setdefault("clips", [])
            return datos
        except (ValueError, OSError):
            pass
    return {"videos": [], "clips": []}


def guardar_historial(h):
    h["videos"] = h["videos"][-400:]
    h["clips"] = h["clips"][-600:]
    HISTORIAL.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# 1. NOTICIA
# ----------------------------------------------------------------------------
def limpiar_texto(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def palabras_clave(t):
    return {w for w in re.findall(r"\w{4,}", t.lower())}


def se_parece(a, b):
    pa, pb = palabras_clave(a), palabras_clave(b)
    if not pa or not pb:
        return False
    return len(pa & pb) / len(pa | pb) > 0.5


def elegir_noticia(historial):
    usados = {v["id"] for v in historial["videos"]}
    titulos_previos = [v["titulo_fuente"] for v in historial["videos"][-60:] if "titulo_fuente" in v]
    candidatos = []
    import feedparser

    for nombre, url in FUENTES:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # una fuente caida no debe detener todo
            print(f"  (fuente {nombre} no disponible: {e})")
            continue
        for e in feed.entries[:25]:
            titulo = limpiar_texto(e.get("title", ""))
            resumen = limpiar_texto(e.get("summary", "") or e.get("description", ""))
            enlace = e.get("link", "")
            if not titulo or len(resumen) < 80:
                continue
            ident = hashlib.sha1((enlace or titulo).encode()).hexdigest()[:16]
            if ident in usados or any(se_parece(titulo, p) for p in titulos_previos):
                continue
            candidatos.append({"id": ident, "titulo": titulo, "resumen": resumen[:1500], "enlace": enlace, "fuente": nombre})
    if not candidatos:
        raise RuntimeError("No encontre noticias nuevas en las fuentes.")
    random.shuffle(candidatos)
    return candidatos[0]


# ----------------------------------------------------------------------------
# 2. GUION CON GROQ
# ----------------------------------------------------------------------------
def llamar_groq(mensajes):
    ultimo = "sin respuesta"
    for modelo in MODELOS_GROQ:
        for intento in range(2):
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    json={
                        "model": modelo,
                        "messages": mensajes,
                        "temperature": 0.7,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=90,
                )
                if r.status_code == 200:
                    return json.loads(r.json()["choices"][0]["message"]["content"])
                ultimo = f"{modelo}: HTTP {r.status_code} {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(6 * (intento + 1))
                    continue
                break  # error de modelo o de clave: probar el siguiente modelo
            except (requests.RequestException, ValueError, KeyError) as e:
                ultimo = f"{modelo}: {e}"
                time.sleep(3)
    raise RuntimeError(f"Groq no respondio: {ultimo}")


def escribir_guion(noticia):
    objetivo = int(SEGUNDOS * PALABRAS_POR_SEGUNDO)
    sistema = (
        "Eres guionista de un canal de YouTube de noticias explicadas, en español neutro y claro. "
        "Reglas estrictas: (1) usa SOLO los hechos que aparecen en el texto de la noticia; no inventes cifras, "
        "nombres, fechas ni citas; (2) escribe todo con palabras nuevas, sin copiar frases del original; "
        "(3) tono serio y sereno, sin sensacionalismo ni clickbait; (4) si falta un dato, dilo con honestidad "
        "(por ejemplo 'aun no hay mas detalles'); (5) estructura: gancho de una frase, que paso, por que importa, "
        "contexto util, cierre breve; (6) sin emojis, sin hashtags, sin acotaciones, sin pedir suscripciones; "
        "(7) el guion es texto corrido listo para ser leido en voz alta."
    )
    usuario = (
        f"Noticia (fuente: {noticia['fuente']}):\nTitular: {noticia['titulo']}\nResumen: {noticia['resumen']}\n\n"
        f"Devuelve SOLO un JSON con estas claves:\n"
        f'"titulo": titulo honesto del video, maximo 90 caracteres;\n'
        f'"guion": narracion de aproximadamente {objetivo} palabras;\n'
        f'"descripcion": 2 o 3 oraciones que resuman el video;\n'
        f'"etiquetas": lista de 5 a 8 palabras clave en español;\n'
        f'"escenas": lista de exactamente {NUM_ESCENAS} busquedas EN INGLES de 1 a 3 palabras para videos de archivo '
        f"(imagenes genericas y concretas, sin nombres propios, sin marcas, sin personas conocidas)."
    )
    for _ in range(2):
        d = llamar_groq([{"role": "system", "content": sistema}, {"role": "user", "content": usuario}])
        guion = limpiar_texto(str(d.get("guion", "")))
        n = len(guion.split())
        escenas = [str(x).strip() for x in d.get("escenas", []) if str(x).strip()]
        if objetivo * 0.6 <= n <= objetivo * 1.5 and d.get("titulo") and escenas:
            etiquetas = [str(x).strip() for x in d.get("etiquetas", []) if str(x).strip()][:8]
            return {
                "titulo": limpiar_texto(str(d["titulo"]))[:95],
                "guion": guion,
                "descripcion": limpiar_texto(str(d.get("descripcion", ""))),
                "etiquetas": etiquetas,
                "escenas": (escenas + BUSQUEDAS_RESPALDO)[:NUM_ESCENAS],
            }
        print(f"  (guion con {n} palabras, objetivo {objetivo}; reintento)")
    raise RuntimeError("Groq no entrego un guion con el largo esperado.")


# ----------------------------------------------------------------------------
# 3. VOZ (Edge TTS) Y SUBTITULOS
# ----------------------------------------------------------------------------
async def _generar_voz(texto, ruta_mp3):
    import edge_tts

    try:
        com = edge_tts.Communicate(texto, VOZ, rate=VELOCIDAD, boundary="WordBoundary")
    except TypeError:  # version antigua de la libreria
        com = edge_tts.Communicate(texto, VOZ, rate=VELOCIDAD)
    marcas = []
    with open(ruta_mp3, "wb") as f:
        async for trozo in com.stream():
            if trozo["type"] == "audio":
                f.write(trozo["data"])
            elif trozo["type"] in ("WordBoundary", "SentenceBoundary"):
                marcas.append((trozo["offset"] / 1e7, trozo["duration"] / 1e7, trozo["text"]))
    return marcas


def generar_voz(texto, ruta_mp3):
    for intento in range(3):
        try:
            marcas = asyncio.run(_generar_voz(texto, ruta_mp3))
            if Path(ruta_mp3).exists() and Path(ruta_mp3).stat().st_size > 2000:
                return marcas
        except Exception as e:
            print(f"  (voz, intento {intento + 1} fallo: {e})")
        time.sleep(4 * (intento + 1))
    raise RuntimeError("No se pudo generar la voz con Edge TTS.")


def duracion(ruta):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(ruta)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def palabras_con_tiempo(marcas, texto, dur_audio):
    """Convierte las marcas de la voz en una lista (inicio, fin, palabra). Si no hay marcas, estima."""
    palabras = []
    for ini, dur, txt in marcas:
        ws = txt.split()
        if not ws:
            continue
        total = sum(len(w) for w in ws) or 1
        t = ini
        for w in ws:
            d = dur * len(w) / total
            palabras.append((t, t + d, w))
            t += d
    if not palabras:  # plan B: repartir por largo de palabra
        ws = texto.split()
        total = sum(len(w) for w in ws) or 1
        t = 0.0
        for w in ws:
            d = dur_audio * len(w) / total
            palabras.append((t, t + d, w))
            t += d
    return palabras


def tiempo_ass(s):
    s = max(0.0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def escapar_ass(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def crear_subtitulos(palabras, dur_total, ruta_ass):
    # grupos de hasta 4 palabras; se corta antes si hay punto o coma
    grupos, actual = [], []
    for p in palabras:
        actual.append(p)
        if len(actual) >= 4 or re.search(r"[.!?;:,]$", p[2]):
            grupos.append(actual)
            actual = []
    if actual:
        grupos.append(actual)

    cabecera = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {ANCHO}\nPlayResY: {ALTO}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Sub,DejaVu Sans,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,0,2,80,80,520,1\n"
        "Style: Marca,DejaVu Sans,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,8,60,60,170,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lineas = [f"Dialogue: 0,{tiempo_ass(0)},{tiempo_ass(dur_total)},Marca,,0,0,0,,{escapar_ass(NOMBRE_CANAL)}"]
    for i, g in enumerate(grupos):
        ini = g[0][0]
        fin = grupos[i + 1][0][0] if i + 1 < len(grupos) else min(dur_total, g[-1][1] + 0.4)
        fin = max(fin, ini + 0.25)
        texto = escapar_ass(" ".join(w[2] for w in g))
        lineas.append(f"Dialogue: 0,{tiempo_ass(ini)},{tiempo_ass(fin)},Sub,,0,0,0,,{texto}")
    Path(ruta_ass).write_text(cabecera + "\n".join(lineas) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# 4. IMAGENES Y VIDEOS LIBRES (Pixabay)
# ----------------------------------------------------------------------------
def buscar_clip(termino, ya_usados):
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_KEY, "q": termino[:90], "per_page": 30, "safesearch": "true"},
            timeout=30,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
    except Exception as e:
        print(f"  (Pixabay fallo con '{termino}': {e})")
        return None
    random.shuffle(hits)
    for h in hits:
        if h.get("id") in ya_usados:
            continue
        for tam in ("large", "medium", "small"):
            u = (h.get("videos", {}).get(tam) or {}).get("url")
            if u:
                return h["id"], u
    return None


def descargar(url, destino):
    with requests.get(url, headers=UA, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = 0
        with open(destino, "wb") as f:
            for trozo in r.iter_content(1 << 20):
                total += len(trozo)
                if total > 120 * (1 << 20):
                    raise RuntimeError("clip demasiado grande")
                f.write(trozo)


def conseguir_clips(escenas, trabajo, historial):
    usados = set(historial["clips"])
    rutas, ids = [], []
    respaldo = list(BUSQUEDAS_RESPALDO)
    for i, termino in enumerate(escenas):
        intentos = [termino] + respaldo
        for t in intentos:
            res = buscar_clip(t, usados)
            if not res:
                continue
            cid, url = res
            destino = trabajo / f"bruto_{i}.mp4"
            try:
                descargar(url, destino)
            except Exception as e:
                print(f"  (descarga fallo: {e})")
                continue
            usados.add(cid)
            ids.append(cid)
            rutas.append(destino)
            break
        else:
            raise RuntimeError(f"No pude conseguir video para la escena {i + 1}.")
    return rutas, ids


# ----------------------------------------------------------------------------
# 5. ARMAR EL VIDEO (FFmpeg)
# ----------------------------------------------------------------------------
def ejecutar(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg fallo: {' '.join(cmd[:4])} ...\n{r.stderr[-800:]}")


def ensamblar(clips, audio, ass, salida, trabajo):
    dur = duracion(audio) + 0.6
    por_escena = dur / len(clips)
    vf = (
        f"scale={ANCHO}:{ALTO}:force_original_aspect_ratio=increase,"
        f"crop={ANCHO}:{ALTO},fps=30,setsar=1,format=yuv420p"
    )
    listos = []
    for i, c in enumerate(clips):
        out = trabajo / f"escena_{i}.mp4"
        ejecutar(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(c), "-t", f"{por_escena:.3f}", "-vf", vf,
                  "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out)])
        listos.append(out)
    lista = trabajo / "lista.txt"
    lista.write_text("".join(f"file '{p.as_posix()}'\n" for p in listos), encoding="utf-8")
    fondo = trabajo / "fondo.mp4"
    ejecutar(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lista), "-c", "copy", str(fondo)])
    ejecutar(["ffmpeg", "-y", "-i", "fondo.mp4", "-i", Path(audio).name, "-vf", f"ass={Path(ass).name}",
              "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
              Path(salida).name], cwd=str(trabajo))


# ----------------------------------------------------------------------------
# 6. SUBIR A YOUTUBE
# ----------------------------------------------------------------------------
def subir_youtube(ruta, guion, noticia):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    descripcion = (
        f"{guion['descripcion']}\n\n"
        f"Fuente de la noticia: {noticia['fuente']} - {noticia['enlace']}\n"
        f"Resumen elaborado por el canal con palabras propias. Voz generada con inteligencia artificial. "
        f"Videos de archivo: Pixabay (licencia libre).\n\n#Shorts #Noticias #Actualidad"
    )[:4900]
    cuerpo = {
        "snippet": {
            "title": guion["titulo"],
            "description": descripcion,
            "tags": guion["etiquetas"],
            "categoryId": "25",
            "defaultLanguage": "es",
            "defaultAudioLanguage": "es",
        },
        "status": {"privacyStatus": PRIVACIDAD, "selfDeclaredMadeForKids": False},
    }
    try:
        req = yt.videos().insert(
            part="snippet,status", body=cuerpo,
            media_body=MediaFileUpload(str(ruta), mimetype="video/mp4", chunksize=-1, resumable=True),
        )
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]
    except HttpError as e:
        raise RuntimeError(f"YouTube rechazo la subida: {e}")


# ----------------------------------------------------------------------------
# FLUJO COMPLETO
# ----------------------------------------------------------------------------
def producir_uno(historial):
    print("1) Buscando noticia...")
    noticia = elegir_noticia(historial)
    print(f"   {noticia['fuente']}: {noticia['titulo']}")
    print("2) Escribiendo guion...")
    guion = escribir_guion(noticia)
    print(f"   {guion['titulo']}  ({len(guion['guion'].split())} palabras)")

    trabajo = Path(tempfile.mkdtemp(prefix="video_"))
    try:
        print("3) Generando voz y subtitulos...")
        mp3 = trabajo / "voz.mp3"
        marcas = generar_voz(guion["guion"], mp3)
        dur = duracion(mp3)
        palabras = palabras_con_tiempo(marcas, guion["guion"], dur)
        crear_subtitulos(palabras, dur + 0.6, trabajo / "subs.ass")
        print(f"   Voz de {dur:.0f} segundos")

        print("4) Descargando videos de archivo...")
        clips, ids = conseguir_clips(guion["escenas"], trabajo, historial)

        print("5) Armando el video...")
        final = trabajo / "final.mp4"
        ensamblar(clips, mp3, trabajo / "subs.ass", final, trabajo)

        print(f"6) Subiendo a YouTube ({PRIVACIDAD})...")
        vid = subir_youtube(final, guion, noticia)
        print(f"   Listo: https://youtu.be/{vid}")
    finally:
        shutil.rmtree(trabajo, ignore_errors=True)

    historial["videos"].append({
        "id": noticia["id"], "titulo_fuente": noticia["titulo"], "titulo": guion["titulo"],
        "fuente": noticia["fuente"], "enlace": noticia["enlace"], "youtube_id": vid,
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    historial["clips"].extend(ids)


def main():
    guardar_historial(carg

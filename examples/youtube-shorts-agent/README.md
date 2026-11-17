# YouTube Shorts Agent

Fasst ein YouTube-Video zusammen und schneidet daraus fertige, untertitelte
Hochkant-Shorts (1080×1920, 30 fps).

```
YouTube-URL ─▶ Transkript ─▶ Claude (Zusammenfassung + Schnittplan) ─▶ ffmpeg ─▶ Shorts + summary.md
```

## Was herauskommt

```
out/<video-id>/
  summary.md                 TL;DR, Zusammenfassung, Kernpunkte mit Zeitstempel-Links, Clip-Tabelle
  shorts/01-highlight-….mp4  die stärksten Momente als ganze Mini-Geschichten, bester zuerst
  shorts/*.json              Titel, Beschreibung, Hashtags, Quell-Zeitstempel für den Upload
```

Jedes Highlight ist ein zusammenhängendes Stück (45–60 s) mit Anfang und Pointe. Es beginnt
mit einer groß eingeblendeten **Catchphrase** und einem kurzen **Teaser** – dem packendsten
Satz aus dem Clip –, danach läuft der Clip von vorn. Jedes Short hat animierte Wort-für-Wort-Untertitel (aktuelles Wort farbig), eine
Hook-Zeile oben, einen Fortschrittsbalken und auf −14 LUFS normalisierten Ton.
Untertitel und Hook liegen außerhalb der Bereiche, die die Shorts-Oberfläche verdeckt.

## Windows: als App

1. Einmalig Python und ffmpeg installieren (PowerShell, danach Fenster neu öffnen):
   `winget install Python.Python.3.12` und `winget install Gyan.FFmpeg`
2. Ordner entpacken und **`start.bat` doppelklicken**. Beim ersten Mal wird alles
   installiert (einige Minuten), danach öffnet sich das App-Fenster.
3. Auf dem Desktop liegt jetzt **„YouTube Shorts Agent“** – ab dann reicht ein Doppelklick darauf.

In der App: Link einfügen → **Shorts erstellen**. Fortschritt, fertige Shorts (Doppelklick
spielt ab), Zusammenfassung und Protokoll stehen im Fenster. Wählst du ein Short aus, siehst du
Titel, Alternativen, Beschreibung und Hashtags; **„Titel & Text kopieren“** legt alles fürs Hochladen
in die Zwischenablage. Der API-Key ist optional und
kann mit „merken“ für deinen Windows-Benutzer gespeichert werden. Videos landen standardmäßig
in `Videos\YouTube Shorts`.

Die Python-Umgebung liegt unter `%LOCALAPPDATA%\YouTubeShortsAgent` (nicht in OneDrive).
`start.bat -Console` startet die Terminal-Version.

Auf anderen Systemen startet `shorts-agent-app` (bzw. `python -m shorts_agent.gui`) dasselbe Fenster.

## Ohne API-Key: kostenlose KI auf dem eigenen PC

Ohne Key nutzt der Agent automatisch eine lokale KI über [Ollama](https://ollama.com) mit dem
freien Modell `qwen2.5:7b` (Deutsch und Englisch). Kein Key, keine Kosten – dafür einmalig
ca. 5–6 GB Download, mindestens 8 GB Arbeitsspeicher und pro Video einige Minuten Rechenzeit
(mit Grafikkarte deutlich schneller).

- **In der App:** Knopf **„Kostenlose KI einrichten“** – installiert Ollama und lädt das Modell.
- **Von Hand:** `winget install Ollama.Ollama`, danach `ollama pull qwen2.5:7b`.

Reihenfolge bei `--analyzer auto`: Claude (wenn ein Key funktioniert) → lokale KI (wenn
eingerichtet) → einfache Auswahl ohne KI. Erzwingen mit `--analyzer local`, anderes Modell mit
`--local-model <name>`.

## Installation

Voraussetzungen: Python ≥ 3.10 und `ffmpeg` (mit libass – bei den Standardpaketen dabei).

```bash
cd examples/youtube-shorts-agent
```

```bash
pip install -e ".[vision]"
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Optional: `pip install -e ".[whisper]"` für Videos ohne Untertitel und lokale Dateien
(Spracherkennung mit faster-whisper).

## Benutzung

```bash
shorts-agent "https://www.youtube.com/watch?v=VIDEO_ID"
```

```bash
# 5 Highlights, kein Recap, deutsche Titel/Zusammenfassung
shorts-agent "https://youtu.be/VIDEO_ID" -n 5 --mode highlights --output-lang Deutsch
```

```bash
# nur die Zusammenfassung, nichts rendern
shorts-agent "https://youtu.be/VIDEO_ID" --summary-only
```

```bash
# lokale Datei (nimmt video.srt/.vtt daneben, sonst Whisper)
shorts-agent ./video.mp4 --lang de
```

Wichtige Optionen (`shorts-agent --help` zeigt alle):

| Option | Wirkung |
|---|---|
| `-n 3` | Anzahl Highlight-Shorts |
| `--mode highlights\|recap\|both` | nur Highlights (Standard), Recap oder beides |
| `--min-len 45 --max-len 60` | Längenfenster in Sekunden |
| `--no-teaser` | kein Teaser vorneweg |
| `--layout auto\|crop\|blur` | `auto`: ein ruhiges Gesicht → 9:16-Zuschnitt darauf, sonst ganzes Bild auf unscharfem Hintergrund |
| `--highlight-color "#FFE500"` | Farbe des gesprochenen Worts und des Balkens |
| `--no-uppercase`, `--font`, `--font-size` | Untertitel-Stil |
| `--no-hook`, `--no-progress-bar` | Catchphrase bzw. Balken abschalten |
| `--model`, `--effort` | Claude-Modell (Standard `claude-opus-5-5`) und Denkaufwand |
| `--analyzer auto\|claude\|local\|heuristic` | welche KI auswählt (Standard `auto`, siehe oben) |
| `--force` | Cache (Transkript, Plan) ignorieren |

## Wie es arbeitet

1. **Transkript** – YouTube-Untertitel in der Originalsprache (automatische bevorzugt,
   weil sie Zeiten pro Wort liefern), sonst Whisper.
2. **Analyse** – Claude bekommt das Transkript als nummerierte Zeilen und liefert per
   Structured Output Zusammenfassung, Kernpunkte und einen Schnittplan. Der Plan
   verweist nur auf Zeilennummern, nie auf frei erfundene Zeitstempel.
3. **Titel & Catchphrase** – ein zweiter, gezielter Durchgang pro Clip: Claude schreibt vier
   Varianten aus unterschiedlichen Blickwinkeln und wählt die, bei der man sofort versteht, worum es
   geht, und weiterschauen will. Die anderen Varianten stehen als Alternativen in der `.json`.
4. **Schnittpunkte** – Der Plan wird auf Wortgrenzen eingerastet, auf das Längenfenster
   gebracht (zu lang → hinten kürzen, zu kurz → verlängern) und von Überschneidungen befreit.
5. **Rendern** – ein ffmpeg-Aufruf pro Short: schneiden, auf 9:16 bringen, Untertitel
   (ASS) einbrennen, Balken, Lautheit.

Transkript und Plan werden unter `out/<id>/.work/` gecacht – ein zweiter Lauf mit anderem
Stil kostet keinen weiteren API-Aufruf.

## Tests

```bash
pip install -e ".[dev,vision]" && pytest
```

Die Suite enthält einen echten ffmpeg-Durchlauf (lokales Testvideo → fertige Shorts).

## Hinweise

- Ohne Claude-Zugang fällt `--analyzer auto` auf die Offline-Heuristik zurück. Die liefert
  brauchbare Schnitte, aber keine redaktionelle Auswahl und keine echten Titel.
- Bei blockierten Anfragen von YouTube hilft `yt-dlp` aktuell zu halten
  (`pip install -U yt-dlp`).
- Verwende nur Videos, an denen du die nötigen Rechte hast.

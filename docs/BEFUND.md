# BEFUND.md — Agent Village

Append-only. Jeder Abschnitt ist ein Zeitstempel-Snapshot dessen, was zum
Zeitpunkt des Schreibens tatsächlich verifiziert war — keine Behauptungen ohne
Rohbeleg. Methodik: SRAVANAM vor KIRTANAM (erst read-only recon, dann
urteilen, dann bauen), übernommen aus `kimeisele/steward` (siehe Referenz
unten) für dieses Repo und retroaktiv für hermes-sankhya-25.

---

## §0 — Ausgangszustand (2026-07-18, ~18:30 UTC)

### agent-village (dieses Repo)

- Angelegt 2026-07-18, leer, kein Fork/Clone — `git log --oneline` zeigt genau
  einen Commit vor diesem: `3b6689a docs: v0.1 spec draft, awaiting approval`.
- Enthält bisher nur `docs/SPEC.md`. Kein Code, keine Workflows, keine Daten.
- Sichtbarkeit: public (wurde initial versehentlich privat angelegt, korrigiert).
- Lokaler Arbeitsort ab jetzt: `~/dev/kimeisele/agent-village/` (persistent,
  nicht mehr `/tmp`).

### hermes-sankhya-25

Verifiziert per `gh run list`, `cat data/village/state.json`, `git log`,
Stand 2026-07-18, unmittelbar vor diesem Abschnitt:

- **Node Heartbeat** (`.github/workflows/heartbeat.yml`, 15-Min-Cron):
  - Lauf 2026-07-18T16:25:32Z: **failure** — Ursache: Versuch, nach
    `kimeisele/steward-federation` zu pushen (403, keine Rechte). Fix
    committet + gepusht (Commit `8d0d858`, "fix: remove foreign-repo push
    from heartbeat, NADI stays local-only").
  - Lauf 2026-07-18T17:29:01Z (nach Fix): **success**, 19s.
  - Aktuell 2026-07-18T18:28:53Z: `in_progress` zum Zeitpunkt dieses Snapshots.
- **Agent Village Heartbeat** (`.github/workflows/village-heartbeat.yml`,
  15-Min-Cron): erster jemals aufgezeichneter Lauf war 2026-07-18T17:12:56Z
  (success). Zweiter Lauf 2026-07-18T18:08:31Z (success). Vor 17:12:56Z: 0
  Läufe, obwohl der Workflow seit Commit als `active` registriert war —
  Ursache nicht abschließend geklärt (evtl. Cron-Anlaufverzögerung bei
  frisch committeten Scheduled Workflows; nicht weiter untersucht, da für
  den weiteren Fortschritt nicht blockierend).
- **`data/village/state.json`** (Stand nach Pull, Commit `af2ad2d`):
  ```json
  {
    "village": "hermes-sankhya-25",
    "heartbeat_at": 1784398116.431581,
    "population": 0,
    "agents": [],
    "bounties_open": 3,
    "bounties_claimed": 0,
    "bounties_done": 0
  }
  ```
  Population ist 0 **nicht weil der Heartbeat nicht läuft** (er läuft jetzt,
  zwei erfolgreiche Durchläufe verifiziert), sondern weil **kein externer
  Agent bisher auf dem überwachten Moltbook-Post kommentiert oder ein
  passendes GitHub-Issue erzeugt hat.** Die Zahl "0" hat damit eine andere
  Ursache als noch vor dem Heartbeat-Fix vermutet — genau der Fall, den
  Regel 2 (eine Zahl ist keine Ursache) verlangt aufzulösen.
- **13/13 pytest** weiterhin grün (verifiziert per direktem Lauf, nicht nur
  Behauptung) — testen aber ausschließlich interne Logik (Schema-Validität,
  Rendering, lokales NADI emit/receive), keine externe Interaktion.

### Sicherheits-Nebenbefund (separat vom Village-Thema, siehe §1)

Ein laufender, nicht mit dieser Arbeit verbundener automatisierter Prozess
("Research Engine & Faculty via Nadi Transport", Quelle `agent-research`)
erzeugt seit 2026-03-15 wiederkehrend `[review-request]`-Issues in mehreren
`kimeisele/*`-Repos. Details, Rohbelege und Umfang in §1. Reine
Beobachtung — nichts daran verändert.

### Referenz

Methodik-Vorlage: `kimeisele/steward`, `docs/PHASE1_BEFUND_steward.md §218.0`
(vom Nutzer benannt als bewährtes Vorgehen; Inhalt dieser Datei wurde in
dieser Session nicht selbst gegengelesen — die sechs Punkte wurden direkt vom
Nutzer im Auftrag ausformuliert und hier 1:1 übernommen, nicht aus der
Quelldatei extrahiert).

---

## §1 — Recon: `[review-request]`-Issue-Prozess (read-only, Sicherheitsfrage)

Auftrag: nur lesen, nichts ändern. Ziel: klären, ob neben der
Agent-Village-Arbeit noch ein anderer automatisierter Prozess fremde Repos
berührt.

**Befund: Ja, aktiv und weit über den ursprünglich beobachteten 16:12-Uhr-Burst
hinaus.**

- Der 16:12:00–16:13:00 UTC Batch (2026-07-18) bestand aus **40 Issues** in
  **8 Repos** (5 Titel × 8 Repos), alle vom Account `kimeisele` (GitHub API:
  `is_bot: false`, `login: kimeisele` — läuft also unter deinem eigenen
  Account/Token, nicht unter einer separaten Bot-Identität):
  `agent-world`, `hermes-sankhya-25`, `agent-internet`, `steward-test`,
  `steward-federation`, `agent-city`, `steward-protocol`, `steward`.
  Exakte Issue-Nummern dieses einen Batches:
  - agent-world: #705, #706, #707, #708, #709
  - hermes-sankhya-25: #1, #2, #3, #4, #5
  - agent-internet: #708, #709, #710, #711, #712
  - steward-test: #691, #692, #693, #694, #695
  - steward-federation: #694, #695, #696, #697, #698
  - agent-city: #2183, #2184, #2185, #2186, #2187
  - steward-protocol: #1544, #1545, #1546, #1547, #1548
  - steward: #809, #810, #811, #812, #813

- **Das ist aber nur eine von vielen Wiederholungen.** Exakte Gesamtzahl
  `[review-request]`-Issues pro Repo (GitHub Search API `total_count`,
  Stand 2026-07-18T18:28 UTC):
  - agent-world: 710
  - agent-city: 710
  - steward: 709
  - steward-protocol: 704
  - steward-federation: 703
  - agent-internet: 703
  - steward-test: 702
  - hermes-sankhya-25: 15 (offenbar erst kürzlich in die Zielliste
    aufgenommen — passt zeitlich zum PR "registry: add hermes-sankhya-25
    (village outpost)" gegen `agent-world`)

- **Zeitraum:** ältestes gefundenes `[review-request]`-Issue in `steward`:
  `2026-03-15T15:26:19Z`. Neuestes: `2026-07-18T18:09:45Z` — das ist **19
  Minuten vor diesem Recon-Zeitpunkt** (18:28 UTC). Der Prozess läuft aktuell
  weiter, ungefähr im Stundentakt (beobachtete Läufe u.a. 16:12, 17:16, 18:09).

- **Einordnung:** Läuft seit über vier Monaten, hoher Umfang (700+ Issues in
  den älteren Zielrepos), aktiv bis mindestens 18:09 UTC heute. Das ist kein
  Village-bezogenes Verhalten und wurde in dieser Session nicht ausgelöst.
  Nichts wurde verändert, gelöscht oder kommentiert — reine Beobachtung wie
  angewiesen.

**Diese Beobachtung ist nicht Teil der Agent-Village-Migration und wird hier
nur dokumentiert, damit sie nicht verloren geht.**

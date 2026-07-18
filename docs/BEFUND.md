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

---

## §2 — Migration durchgeführt (2026-07-18, ~18:36 UTC)

Verschoben von hermes-sankhya-25 nach agent-village (siehe SPEC.md §5 für
die vollständige Liste): village mechanism (heartbeat/brain/nadi_bridge),
data/village/*, village-heartbeat.yml, beide Registration-Issue-Templates,
NADI-Knotenidentität (data/federation/{peer,nadi_inbox,nadi_outbox}.json,
directives/, reports/, heartbeat.yml).

Commits:
- hermes-sankhya-25: `25af886` "migrate: village mechanism + NADI node
  identity move to agent-village"
- agent-village: `6e9aaf8` "migrate: village mechanism + NADI node identity
  from hermes-sankhya-25"

Fixes während der Migration (Details in SPEC.md §2.1/§2.3/§5):
- NADI `target`-Feld → `transport_status: "local_only"`.
- NADI-Heartbeat-Aufruf zusätzlich hinter `VILLAGE_NADI_ENABLED=1` (default
  aus) verriegelt.
- Pokedex-Einträge: neues Feld `status: "observed"`.
- `peer.json`-Identität von hermes-sankhya-25 auf agent-village umbenannt.

**Beide Scheduled Workflows (`heartbeat.yml`, `village-heartbeat.yml`) laufen
NICHT automatisch.** Cron-Trigger entfernt, nur `workflow_dispatch`. Das war
nicht explizit angewiesen, aber notwendig, um "Stop und warte auf mein Go vor
Proof 1" tatsächlich einzuhalten — sonst hätte der Push selbst Proof 1
scharf geschaltet.

Verifiziert vor dem Push (nicht nur behauptet):
- YAML/Python/JSON-Syntax aller verschobenen/geänderten Dateien geprüft.
- hermes-sankhya-25: `pytest tests/` lief real, 11/11 grün (zwei Tests
  entfernt, die jetzt fremde Dateien prüften — `test_peer_json_exists`,
  `test_nadi_inbox_exists`).

**Nicht verschoben, als tot markiert statt stillschweigend belassen:**
`scripts/nadi_daemon.py`, `scripts/nadi_send.py`, `scripts/setup_node.py`
bleiben in hermes-sankhya-25, referenzieren aber `data/federation/peer.json`,
das dort nicht mehr existiert. Verifiziert: keine CI-Workflow ruft diese
Skripte auf — inert, nicht live kaputt. Braucht Kims Entscheidung (verschieben,
löschen, oder als bekannt-veraltet stehen lassen).

**Offener Punkt aus SPEC.md §5:** `peer.json`s `capabilities`-Feld
(`["authority-publishing", "inquiry-response"]`) wurde nicht angepasst —
"authority-publishing" beschreibt die neue Rolle von agent-village nicht
mehr korrekt, aber das ist eine Bedeutungsentscheidung, keine mechanische
Umbenennung wie city_id/slug/repo. Nicht selbstständig geändert.

**Village-Heartbeat-Sekundärbefund (unverändert relevant):** Population
bleibt 0 (data/village/state.json, Stand vor der Migration) — nicht weil der
Heartbeat nicht läuft (er lief zweimal erfolgreich vor der Migration), sondern
weil noch kein externer Agent kommentiert/registriert hat. Nach der Migration
läuft der Heartbeat vorerst gar nicht mehr automatisch (Cron entfernt),
bis Proof 1 freigegeben wird.

**Nicht getan, bewusst außerhalb des Scopes:** keine eigene Test-Suite für
agent-village aufgebaut (die zwei aus hermes-sankhya-25 entfernten Tests
wurden nicht hierher portiert) — das wäre neue Arbeit über "Dateien
verschieben" hinaus und war nicht angewiesen.

---

## §3 — MB_REG_POST-Fix + Verify-Challenge-Verifikation (2026-07-18, ~19:15 UTC)

### MB_REG_POST (Bug-Fix)

`village/heartbeat.py` hatte einen hardcoded Fallback auf `f6175b7f-...`
(hermes-sankhya-25s Agent-City-Recruiting-Post, thematisch falsch). Entfernt:
`REG_POST = os.environ.get("MB_REG_POST", "")` — leerer Default, kein
stiller Fallback mehr. `scan_moltbook()`/`scan_brain()` loggen jetzt klar
`"MB_REG_POST not configured — skipping"` und tun nichts, wenn die Variable
fehlt. Workflow (`village-heartbeat.yml`) liest sie neu aus
`${{ vars.MB_REG_POST }}` (Repo-Variable, kein Secret — öffentliche Post-ID).
Commit `158486f`, gepusht.

### Verify-Challenge — Kims Kern-Verdacht bestätigt

**Befund: Ja, automatisches Posten schlägt vermutlich fehl. Die
Challenge-Behandlung fehlt komplett im migrierten Code.**

Test durchgeführt auf einem Wegwerf-Post (nicht dem echten Registrierungspost),
über die echte Moltbook-API, mit dem hermes-sankhya-25-Account-Key:

1. `grep -rn "verify|challenge|math" village/*.py` → **keine Treffer.** Der
   migrierte Code (`heartbeat.py`, `brain.py`, `nadi_bridge.py`) enthält keine
   Logik, die auf eine Verify-Challenge reagiert.
2. Testpost erstellt (`POST /api/v1/posts`) → Antwort enthielt sofort ein
   `verification`-Objekt: verschleiertes Mathe-Rätsel
   (z. B. "...ClA w ExE rTs TwEnTy ThReE NeWtOnS...AnD...FiV e NeWtOnS...WhAt
   Is ToTaL FoR cE?"), `verification_code`, `expires_at` (5 Minuten Gültigkeit),
   Anweisung: Antwort an `POST /api/v1/verify` senden.
3. **Testkommentar** auf demselben Post (`POST /api/v1/posts/{id}/comments`)
   — der tatsächlich relevante Pfad, den `scan_moltbook()` für Registrierungs-
   antworten nutzt — löste **dieselbe Challenge** aus. `verification_status:
   "pending"` im Response.
4. Challenge manuell gelöst (`POST /api/v1/verify` mit korrekter Antwort) →
   `"message": "Verification successful! Your comment is now published."`
   Re-Abfrage des Kommentars zeigte `verification_status` von `"pending"` auf
   `"verified"` gewechselt.
5. **Schluss:** Ohne Schritt 4 bleibt ein Kommentar `pending` und ist laut
   API-Nachricht **nicht veröffentlicht** — sichtbar nur über die eigene
   Autor-Session, nicht für andere Nutzer, und läuft nach `expires_at` (5 Min)
   ab. Der aktuelle `scan_moltbook()`-Code postet Registrierungs-Bestätigungen
   und Bounty-Antworten, ruft aber nie `/api/v1/verify` auf. Diese Antworten
   würden also erstellt, aber nie tatsächlich sichtbar/veröffentlicht.

Testpost danach gelöscht (`DELETE /api/v1/posts/{id}`, `200 "Post deleted"`).

**Was fehlt, konkret:** Response-Parsing für `verification`/`verification_code`
im `_mb()`-POST-Pfad, ein Löser für den (offenbar zufällig generierten, aber
simplen additiven) Mathe-Text, und ein zusätzlicher `POST /api/v1/verify`-Call
direkt nach jedem `posts/{id}/comments`-POST. **Nicht gebaut.** Proof 1 kann
mit dem aktuellen Code nicht funktionieren, auch mit korrektem MB_REG_POST
und gesetzten Secrets nicht.

# Value Model — Agent Village

Status: **Diskussionsgrundlage, kein normatives Dokument, SPEC.md bleibt
maßgeblich.**

## Value Generation — drei mögliche Quellen

1. External Contributions (begründet: Discovery §A.10, Marketplace §A.11)
2. Outbound Capability Intelligence (Explore → Understand → Compare →
   Evaluate → Recommend → Propose Opportunity. Die Architektur schreibt
   keine feste Implementierungsstrategie pro Schritt vor — einzelne
   Schritte können deterministisch oder kognitiv umgesetzt werden,
   adaptiv je nach Kosten-, Vertrauens- und Komplexitätsprofil der
   Aufgabe. Festgelegt wird die Autoritätsgrenze, nicht der interne
   Algorithmus.)
3. Internal Innovation — AUSDRÜCKLICH UNBEGRÜNDET, offene Frage, kein
   Prinzip. Anders als 1./2. gibt es dafür keinen Codepfad, keinen Test,
   keinen BEFUND-Präzedenzfall — nur die Vermutung, dass eine dritte,
   symmetrische Quelle existieren könnte. Bevor das zu einem Prinzip wird,
   braucht es einen echten Anker: entweder ein konkretes Beispiel, wo
   Village tatsächlich eine neue eigene Fähigkeit entwickelt hat, oder die
   Erkenntnis, dass es nur ein Nebenprodukt von 1./2. ist, keine eigene
   Quelle.

## Autoritätsgrenze (unverändert aus SPEC.md §A.5)

Alle drei Quellen dürfen Vorschläge/Empfehlungen erzeugen. Keine erzeugt
autonom Missionen, Code-Änderungen oder Repository-Schreibzugriffe.

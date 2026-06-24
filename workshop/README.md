# KI-Workshop – Präsentation

Foliendeck für einen Workshop mit kleinen Unternehmern: Einstieg über digitale
Transformation und Prozessoptimierung, der Faktor Mensch (Akzeptanz/Change),
eine Live-Demo zu KI-gestütztem Coding und abschließend Produkt-Showcase +
Angebot.

## Dateien

- `KI-Workshop.pptx` – das fertige Deck (16:9, 23 Folien, mit Sprechernotizen).
- `build_deck.py` – Generator. Erzeugt das `.pptx` reproduzierbar neu.

## Neu erzeugen / anpassen

```bash
pip install python-pptx
python build_deck.py     # schreibt KI-Workshop.pptx
```

Inhalt, Reihenfolge, Farben und Texte werden im Skript bearbeitet — so bleibt
das Deck versionierbar.

## Vor dem Vortrag anpassen

- Platzhalter ersetzen: `[Ihr Name]`, `[Ihr Unternehmen]`, `[E-Mail-Adresse]`,
  `[Telefon / Website]`.
- Auf der Abschlussfolie einen echten QR-Code (z. B. Calendly) einsetzen.
- Die Kennzahlen-Folie ("KI in Zahlen") enthält branchenübliche Richtwerte –
  kurz gegenprüfen/aktualisieren und als grobe Größenordnung präsentieren.
- Für die Live-Demo ein Backup (Screenshots oder Aufnahme) bereithalten, falls
  WLAN/Live nicht mitspielt. Details stehen in den Notizen der Demo-Folie.

## Aufbau (roter Faden)

1. Titel & Agenda
2. Vorstellung + Referenzen (Produkte als Vertrauens-Anker)
3. **01 Digitale Transformation** – Grundlagen, Zahlen, Timing
4. **02 Prozesse verstehen** – erst verstehen, dann automatisieren; Potenzialanalyse
5. **03 Der Faktor Mensch** – Technik vs. Mensch, Akzeptanz schaffen
6. **04 KI-gestütztes Coding** – Möglichkeiten, Setup, Live-Demo, Einordnung
7. **05 Nächster Schritt** – Angebot + Call to Action

Die Sprechernotizen zu jeder Folie enthalten Hinweise zur Moderation.

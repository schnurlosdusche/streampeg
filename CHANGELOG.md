# Changelog

## 0.0.52a
- Library: Spaltenbreiten fixiert (table-layout: fixed) — Title bekommt verbleibende Breite, Papierkorb/+ minimal (28px), Playlist 120px, Datum 100px (dd.mm.YY HH:mm)
- Library: Datum-Format auf dd.mm.YY HH:mm erweitert

## 0.0.51a
- Heart/Favorite-Button funktioniert jetzt auch ohne aktive Aufnahme beim Live-Hören (Browser-Player und Cast-Player)
- Neue `stream_favorites` Tabelle: Favoriten werden mit Zeitstempel, Stream-Name und Cover-URL gespeichert
- API: `/api/stream-favorites/toggle` (POST), `/api/stream-favorites` (GET), `/api/stream-favorites/<id>` (DELETE)
- Library: Neue "Datum"-Spalte zeigt Aufnahmedatum (basierend auf Datei-mtime), sortierbar per Klick
- `/api/library/track/find` prüft jetzt auch `stream_favorites` als Fallback

## 0.0.50a
- Library: Playlist-Zuweisung/Entfernung über den Player aktualisiert jetzt sofort die Playlist-Tags in der Track-Tabelle

## 0.0.49a
- Library: Page refresh auf /library zeigt jetzt korrekt die Ordner-Liste an (t()-Funktion war beim harten Refresh noch nicht verfügbar, initLibrary wartet jetzt auf DOMContentLoaded)

## 0.0.48a
- Dashboard: Initialer Seitenaufruf drastisch beschleunigt — keine NAS-Zugriffe mehr beim Rendern, Dateizählung nur noch aus Cache (wird per SSE-Poll nachgeladen)

## 0.0.41a
- Dashboard: Size-Spalte von 75px auf 85px verbreitert

## 0.0.40a
- Dashboard: "GB" in kleiner Schrift hinter dem Wert in der Size-Spalte

## 0.0.39a
- Dashboard: Status-Spalte von 6% auf 8% verbreitert
- Dashboard: Status-Text nicht mehr bold sondern normal

## 0.0.38a
- Dashboard: Split-Table-Header rückgängig gemacht, wieder eine einzelne Tabelle
- Dashboard: Tracks- und Size-Spalte auf je 75px fixiert
- Dashboard: Size-Spaltenüberschrift von "Size (GB)" zu "Size" geändert
- Layout: Nav mit Logo und Menü fixiert (scrollt nicht mit), nur main-Content scrollbar, Player unten fixiert
- Logo 25% kleiner (75px → 56px)

## 0.0.37a
- Dashboard: Disk-Info (Worker/NAS freier Platz) entfernt
- Dashboard: Neue Spalte "Size (GB)" zeigt Gesamtgröße der Downloads pro Stream
- Dashboard: "Streams" Heading entfernt
- Dashboard: Header fixiert (sticky), nur die Stream-Liste scrollt
- Dashboard: Status- und Tracks-Spalte auf je 6% reduziert für Platz

## 0.0.28a
- Track-Info im Cast-Player: Bei LMS wird jetzt der Titel direkt vom LMS-Server abgefragt (status-Kommando mit remoteMeta) statt über ICY — liefert echten Songtitel + Artist + Cover-Art
- Neue Funktion `get_cast_track_info()` wählt automatisch die beste Quelle je Gerätetyp (LMS: Server-API, Sonos: ICY-Fallback)

## 0.0.27a
- ICY Background-Poller: Neuer dauerhafter Background-Thread pollt alle 10s ICY-Metadata für aktive Casts ohne Recording — Track-Info erscheint jetzt zuverlässig auch ohne aktive Aufnahme
- ICY-Check im Player-API unabhängig von `running`-Status — auch bei laufendem Recording ohne Track wird ICY als Fallback genutzt

## 0.0.26a
- Fix: Track-Titel im Cast-Player bei nicht-recordenden Streams — Player-API-Daten (inkl. ICY) werden jetzt bevorzugt vor SSE-Status-Daten, die bei gestopptem Recording leer sind

## 0.0.25a
- Fix: ICY Track-Info bei nicht-recordenden Streams — erster Fetch ist jetzt synchron statt im Background-Thread, damit der Track sofort im Player erscheint

## 0.0.24a
- Fix: Sonos Resume nach Pause — nutzt jetzt DIDL-Lite Metadata beim Replay (UPnP Error 714 behoben)
- Fix: Multiroom-Icon war hinter absolut positionierter Volume-Section verdeckt — `.player-right` hat jetzt z-index:5

## 0.0.20a
- Fix: Multiroom-Icon und Device-Name in eigenen `.player-right` Container rechts am Rand
- Fix: Sonos-Pause/Resume — Streams werden bei Resume neu gestartet (play_uri) statt nur play(), da Streams nach Pause den Puffer verlieren
- Fix: Track-Titel nutzt jetzt `flex:1` mit `max-width: calc(50% - 200px)` und 20px Padding zu den Controls

## 0.0.16a
- Sonos: Speaker wird vor Play automatisch aus Gruppe gelöst (unjoin), damit er unabhängig als eigener Coordinator spielen kann
- Sonos Stop/Pause: Nutzt den Coordinator des Speakers falls er noch in einer Gruppe ist

## 0.0.15a
- Fix: Multiroom-Icon und Device-Name nach rechts verschoben (margin-left:auto) — waren durch absolute Positionierung der Volume-Section aus dem Flow gerutscht

## 0.0.14a
- Fix: "Error loading devices" — Variable `activeDeviceId` in `_renderCastMenu` war noch im alten Singular-Format statt `activeDeviceIds` (Array), verursachte ReferenceError

## 0.0.13a
- Fix: Migration alter Cast-Daten (stream_id→device_id) auf neues Format (device_id→stream_id) beim Laden

## 0.0.12a
- Multi-Cast Fix: Datenstruktur von `stream_id→device_id` auf `device_id→stream_id` umgebaut — selber Stream kann nun auf mehrere Geräte gleichzeitig gecastet werden
- Sonos Fix: `sonos_play()` versucht zuerst einfaches `play_uri`, dann Fallback mit DIDL-Lite Metadata; Fehler werden jetzt zurückgegeben statt verschluckt
- Player-Stop sendet jetzt `device_id` statt `stream_id` — stoppt nur den spezifischen Player, nicht alle Geräte
- Pause/Play-State ist jetzt pro Device statt pro Stream
- Controls + Volume absolut zentriert im Player (unabhängig von Info-Breite und Device-Name)

## 0.0.7a
- Player-Zentrierung: .player-info auf feste Breite (200px) gesetzt, Volume-Controls nun tatsächlich mittig im Player
- Pause/Play-Toggle: Pause-Button zeigt Play-Icon wenn pausiert, klickt man erneut wird fortgesetzt
- Stop-Verhalten: Nach Stop wird Pause-State zurückgesetzt
- Gap von 10px zwischen Volume-Slider und +/- Buttons

## 0.0.3a
- Player-Layout: Pause/Stop-Buttons direkt links neben den Volume-Slider verschoben (zentrierte Einheit)
- Volume-Slider Handle: Orange Kreis ersetzt durch blaues Quadrat mit gleicher Höhe wie die grüne Spur

## 0.0.2a
- Cast-Player: Pause-Button hinzugefügt (Stream pausieren/fortsetzen)
- Device-Name im Player auf weiß (#ddd) geändert für bessere Lesbarkeit
- Volume-Slider explizit mittig im Player positioniert

## 0.0.1a
- Multi-Cast-Player: Bis zu 4 gleichzeitige Cast-Player am unteren Bildschirmrand
- ICY-Metadata-Polling: Track-Info wird auch bei nicht aufnehmenden Casts angezeigt
- BPM- und Tonarterkennung im Autotagging (aubio + Krumhansl-Schmuckler)
- Vollständige i18n-Umstellung: Alle deutschen Hardcode-Strings durch t()-System ersetzt (6 Sprachen: en, de, fr, es, pl, it)
- YouTube-Download: Einzelvideos und Playlists herunterladen mit Konvertierung zu MP3
- YouTube-Playlist-Erkennung: Prefix-basiert (PL/OL/FL = Playlist, RD/UU = kein Playlist)
- Versionsanzeige unten rechts (zentral definiert in app.py)
- config.py aus Git-Tracking entfernt, config.example.py als Vorlage erstellt
- Start All / Stop All Buttons dauerhaft sichtbar oberhalb der Actions
- Hintergrund-Konvertierung: MP3-Konvertierung läuft weiter bei Seitenwechsel
- Track-Info-Spalte verkleinert für saubere Header-Darstellung

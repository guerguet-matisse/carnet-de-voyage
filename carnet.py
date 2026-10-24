#!/usr/bin/env python3
"""Carnet de voyage : un seul fichier, zéro dépendance.

  python carnet.py          -> application (fenêtre dédiée, interface moderne)
  python carnet.py web      -> même interface dans ton navigateur (--port 8000, --no-browser)
  python carnet.py --help   -> toutes les commandes en ligne de commande

Optimisations : SQLite en mode WAL, index composite, recherche plein texte FTS5
(insensible aux accents), statistiques calculées en SQL, serveur local sans dépendance.
"""
import argparse
import base64
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from collections import defaultdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB_PATH = Path.home() / ".carnet_voyage.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS voyage(
  id INTEGER PRIMARY KEY,
  nom TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pays TEXT, debut TEXT, fin TEXT
);
CREATE TABLE IF NOT EXISTS note(
  id INTEGER PRIMARY KEY,
  voyage_id INTEGER NOT NULL REFERENCES voyage(id) ON DELETE CASCADE,
  jour TEXT NOT NULL, lieu TEXT, texte TEXT NOT NULL,
  humeur INTEGER CHECK(humeur BETWEEN 1 AND 5),
  depense REAL NOT NULL DEFAULT 0, tags TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_voyage_jour ON note(voyage_id, jour);
CREATE TABLE IF NOT EXISTS reglage(cle TEXT PRIMARY KEY, valeur TEXT);
CREATE TABLE IF NOT EXISTS photo(
  id INTEGER PRIMARY KEY,
  voyage_id INTEGER NOT NULL REFERENCES voyage(id) ON DELETE CASCADE,
  jour TEXT NOT NULL, grande BLOB NOT NULL, mini BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS idx_photo_voyage_jour ON photo(voyage_id, jour);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
  lieu, texte, tags, content='note', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS note_ai AFTER INSERT ON note BEGIN
  INSERT INTO note_fts(rowid,lieu,texte,tags) VALUES(new.id,new.lieu,new.texte,new.tags);
END;
CREATE TRIGGER IF NOT EXISTS note_ad AFTER DELETE ON note BEGIN
  INSERT INTO note_fts(note_fts,rowid,lieu,texte,tags)
  VALUES('delete',old.id,old.lieu,old.texte,old.tags);
END;
CREATE TRIGGER IF NOT EXISTS note_au AFTER UPDATE ON note BEGIN
  INSERT INTO note_fts(note_fts,rowid,lieu,texte,tags)
  VALUES('delete',old.id,old.lieu,old.texte,old.tags);
  INSERT INTO note_fts(rowid,lieu,texte,tags) VALUES(new.id,new.lieu,new.texte,new.tags);
END;
"""


class Base(sqlite3.Connection):
    has_fts = False


def migrer(db):
    """Ajoute les colonnes apparues depuis les anciennes versions (les données existantes sont conservées)."""
    def colonnes(table):
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    if "favori" not in colonnes("note"):
        db.execute("ALTER TABLE note ADD COLUMN favori INTEGER NOT NULL DEFAULT 0")
    for nom, typ in (("budget", "REAL"), ("couverture", "INTEGER")):
        if nom not in colonnes("voyage"):
            db.execute(f"ALTER TABLE voyage ADD COLUMN {nom} {typ}")
    db.commit()


def connect():
    db = sqlite3.connect(DB_PATH, factory=Base, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrer(db)
    try:
        db.executescript(FTS)
        db.has_fts = True
    except sqlite3.OperationalError:  # FTS5 absent : repli sur LIKE
        db.has_fts = False
    return db


def montant(s):
    """Accepte 12.5, 12,5, « 1 500,50 » et « 12 € »."""
    return float(re.sub(r"\s+", "", s).replace(",", ".").replace("€", ""))


def iso(s):
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        sys.exit(f"Date invalide « {s} » (format attendu : AAAA-MM-JJ)")


def voyage_id(db, nom):
    row = db.execute("SELECT id FROM voyage WHERE nom=?", (nom,)).fetchone()
    if not row:
        sys.exit(f"Voyage « {nom} » introuvable. Crée-le avec : voyage \"{nom}\"")
    return row["id"]


def fmt_note(n):
    humeur = "★" * (n["humeur"] or 0)
    ligne = f"[{n['id']}] {n['jour']}"
    if n["lieu"]:
        ligne += f" · {n['lieu']}"
    if humeur:
        ligne += f" · {humeur}"
    if n["favori"]:
        ligne += " · ⭐ moment fort"
    if n["depense"]:
        ligne += f" · {n['depense']:.2f} €"
    if n["tags"]:
        ligne += f" · #{n['tags'].replace(',', ' #')}"
    return f"{ligne}\n    {n['texte']}"


# ---------- commandes ----------
def cmd_voyage(db, a):
    debut = iso(a.debut) if a.debut else None
    fin = iso(a.fin) if a.fin else None
    with db:
        db.execute(
            "INSERT INTO voyage(nom,pays,debut,fin,budget) VALUES(?,?,?,?,?) "
            "ON CONFLICT(nom) DO UPDATE SET pays=COALESCE(excluded.pays,pays), "
            "debut=COALESCE(excluded.debut,debut), fin=COALESCE(excluded.fin,fin), "
            "budget=COALESCE(excluded.budget,budget)",
            (a.nom, a.pays, debut, fin, a.budget),
        )
    print(f"Voyage « {a.nom} » enregistré.")


def cmd_voyages(db, a):
    rows = db.execute(
        "SELECT v.nom,v.pays,v.debut,v.fin,COUNT(n.id) nb,COALESCE(SUM(n.depense),0) total "
        "FROM voyage v LEFT JOIN note n ON n.voyage_id=v.id GROUP BY v.id ORDER BY v.debut DESC"
    ).fetchall()
    if not rows:
        print("Aucun voyage. Crée-en un avec : voyage \"Japon\" --pays Japon")
    for r in rows:
        dates = f"{r['debut'] or '?'} → {r['fin'] or '?'}"
        print(f"• {r['nom']} ({r['pays'] or '—'}) {dates} | {r['nb']} notes | {r['total']:.2f} €")


def cmd_note(db, a):
    vid = voyage_id(db, a.voyage)
    with db:
        cur = db.execute(
            "INSERT INTO note(voyage_id,jour,lieu,texte,humeur,depense,tags,favori) VALUES(?,?,?,?,?,?,?,?)",
            (vid, iso(a.jour) if a.jour else date.today().isoformat(), a.lieu,
             a.texte, a.humeur, a.depense, a.tags, 1 if a.favori else 0),
        )
    print(f"Note #{cur.lastrowid} ajoutée.")


def cmd_lire(db, a):
    vid = voyage_id(db, a.voyage)
    rows = db.execute(
        "SELECT * FROM note WHERE voyage_id=? ORDER BY jour, id LIMIT ?", (vid, a.limite)
    ).fetchall()
    for n in rows:
        print(fmt_note(n))
    if not rows:
        print("Aucune note pour ce voyage.")


def rechercher(db, requete, limite=20, vid=None):
    """Recherche plein texte (FTS5) ou repli LIKE ; filtrable par voyage."""
    extra, args = ("", ()) if vid is None else (" AND n.voyage_id=?", (vid,))
    if db.has_fts:
        q = " ".join('"%s"*' % t.replace('"', "") for t in requete.split())
        if not q:
            return []
        return db.execute(
            "SELECT n.* FROM note_fts f JOIN note n ON n.id=f.rowid "
            "WHERE note_fts MATCH ?" + extra + " ORDER BY rank LIMIT ?",
            (q, *args, limite),
        ).fetchall()
    like = f"%{requete}%"
    return db.execute(
        "SELECT n.* FROM note n WHERE (n.texte LIKE ? OR n.lieu LIKE ? OR n.tags LIKE ?)"
        + extra + " LIMIT ?",
        (like, like, like, *args, limite),
    ).fetchall()


def cmd_chercher(db, a):
    rows = rechercher(db, a.requete, a.limite)
    for n in rows:
        print(fmt_note(n))
    if not rows:
        print("Aucun résultat.")


def cmd_stats(db, a):
    where, args = "", ()
    if a.voyage:
        where, args = "WHERE voyage_id=?", (voyage_id(db, a.voyage),)
    g = db.execute(
        f"SELECT COUNT(*) nb, COALESCE(SUM(depense),0) total, AVG(humeur) h, "
        f"COUNT(DISTINCT lieu) lieux, MIN(jour) d, MAX(jour) f FROM note {where}", args
    ).fetchone()
    print(f"Notes : {g['nb']} | Lieux : {g['lieux']} | Dépenses : {g['total']:.2f} €")
    if g["h"]:
        print(f"Humeur moyenne : {g['h']:.1f}/5 | Période : {g['d']} → {g['f']}")
    top = db.execute(
        f"SELECT lieu, COUNT(*) nb, SUM(depense) s FROM note {where} "
        f"{'AND' if where else 'WHERE'} lieu IS NOT NULL GROUP BY lieu ORDER BY nb DESC LIMIT 5", args
    ).fetchall()
    for t in top:
        print(f"  - {t['lieu']} : {t['nb']} notes, {t['s']:.2f} €")


def cmd_suppr(db, a):
    with db:
        cur = db.execute("DELETE FROM note WHERE id=?", (a.id,))
    print("Note supprimée." if cur.rowcount else "Note introuvable.")


def exporter(db, nom, vid, fmt="md", images=None):
    rows = [dict(r) for r in db.execute(
        "SELECT jour,lieu,texte,humeur,depense,tags,favori FROM note WHERE voyage_id=? ORDER BY jour,id", (vid,))]
    if fmt == "json":
        return json.dumps({"voyage": nom, "notes": rows}, ensure_ascii=False, indent=2)
    par_jour = defaultdict(list)
    for r in rows:
        par_jour[r["jour"]].append(r)
    for j in images or {}:
        par_jour.setdefault(j, [])
    parts = [f"# {nom}\n"]
    for jour, notes in sorted(par_jour.items()):
        parts.append(f"## {jour}\n")
        for chemin in (images or {}).get(jour, []):
            parts.append(f"![Photo du {jour}]({chemin})\n")
        for r in notes:
            lieu = ("⭐ " if r["favori"] else "") + (f"**{r['lieu']}** — " if r["lieu"] else "")
            extra = f" _({r['depense']:.2f} €)_" if r["depense"] else ""
            parts.append(f"- {lieu}{r['texte']}{extra}")
        parts.append("")
    return "\n".join(parts)


def sauvegarde_octets(db):
    """Copie cohérente de toute la base (notes, photos, réglages) via l'API de sauvegarde de SQLite."""
    with tempfile.TemporaryDirectory() as tmp:
        chemin = Path(tmp) / "carnet.db"
        dest = sqlite3.connect(chemin)
        try:
            db.backup(dest)
        finally:
            dest.close()
        return chemin.read_bytes()


def cmd_sauvegarde(db, a):
    chemin = Path(a.sortie or f"carnet-sauvegarde-{date.today().isoformat()}.db")
    chemin.write_bytes(sauvegarde_octets(db))
    print(f"Sauvegarde enregistrée : {chemin}")


def cmd_export(db, a):
    out = exporter(db, a.voyage, voyage_id(db, a.voyage), a.format)
    if a.sortie:
        Path(a.sortie).write_text(out, encoding="utf-8")
        print(f"Exporté vers {a.sortie}")
    else:
        print(out)


def build_parser():
    p = argparse.ArgumentParser(prog="carnet", description="Carnet de voyage en ligne de commande")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("voyage", help="créer / mettre à jour un voyage")
    s.add_argument("nom"); s.add_argument("--pays"); s.add_argument("--debut"); s.add_argument("--fin")
    s.add_argument("--budget", type=montant, help="budget total en euros")
    s.set_defaults(fn=cmd_voyage)

    sub.add_parser("voyages", help="lister les voyages").set_defaults(fn=cmd_voyages)

    s = sub.add_parser("note", help="ajouter une note")
    s.add_argument("voyage"); s.add_argument("texte")
    s.add_argument("--lieu"); s.add_argument("--jour", help="AAAA-MM-JJ (défaut : aujourd'hui)")
    s.add_argument("--humeur", type=int, choices=range(1, 6))
    s.add_argument("--depense", type=montant, default=0.0)
    s.add_argument("--tags", help="séparés par des virgules")
    s.add_argument("--favori", action="store_true", help="marquer comme moment fort")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("lire", help="lire les notes d'un voyage")
    s.add_argument("voyage"); s.add_argument("--limite", type=int, default=100)
    s.set_defaults(fn=cmd_lire)

    s = sub.add_parser("chercher", help="recherche plein texte")
    s.add_argument("requete"); s.add_argument("--limite", type=int, default=20)
    s.set_defaults(fn=cmd_chercher)

    s = sub.add_parser("stats", help="statistiques")
    s.add_argument("voyage", nargs="?"); s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("suppr", help="supprimer une note par ID")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_suppr)

    s = sub.add_parser("export", help="exporter en Markdown ou JSON")
    s.add_argument("voyage"); s.add_argument("--format", choices=["md", "json"], default="md")
    s.add_argument("-o", "--sortie"); s.set_defaults(fn=cmd_export)

    s = sub.add_parser("sauvegarde", help="copie complète du carnet (notes, photos, réglages)")
    s.add_argument("-o", "--sortie", help="fichier .db à créer (défaut : carnet-sauvegarde-AAAA-MM-JJ.db)")
    s.set_defaults(fn=cmd_sauvegarde)

    s = sub.add_parser("web", help="version web locale (navigateur)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_web)

    sub.add_parser("gui", help="application (fenêtre dédiée)").set_defaults(fn=cmd_gui)
    return p


# ======================= INTERFACE (HTML/CSS/JS) =======================
APP_HTML = r'''<!doctype html>
<html lang="fr" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Carnet de voyage</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2315275a'/%3E%3Ccircle cx='32' cy='32' r='17' fill='none' stroke='%23fff' stroke-width='3'/%3E%3Cpath d='M32 15l7 17-7 17-7-17z' fill='%23d62839'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#edf0f5;--surface:#ffffff;--raised:#f6f8fc;--ink:#101b34;--muted:#566280;--line:#d2d9e6;
  --blue:#2447b5;--blue-ink:#ffffff;--red:#d62839;--hero:#15275a;--sky:#e1e9fa;
  --display:"Bahnschrift","DIN Alternate","Roboto Condensed","Arial Narrow",system-ui,sans-serif;
  --text:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",sans-serif;
}
:root[data-theme="dark"]{
  --bg:#0b1326;--surface:#111c37;--raised:#16244a;--ink:#e7ecf8;--muted:#98a5c3;--line:#253558;
  --blue:#86a8ff;--blue-ink:#0b1326;--red:#ff6b7d;--hero:#17295f;--sky:#1b2c55;
}
@media (prefers-color-scheme:dark){
  :root[data-theme="auto"]{
    --bg:#0b1326;--surface:#111c37;--raised:#16244a;--ink:#e7ecf8;--muted:#98a5c3;--line:#253558;
    --blue:#86a8ff;--blue-ink:#0b1326;--red:#ff6b7d;--hero:#17295f;--sky:#1b2c55;
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--text);-webkit-font-smoothing:antialiased}
button,input,textarea,select{font:inherit;color:inherit}
button{cursor:pointer}
a{color:inherit}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
[hidden]{display:none!important}

.app{display:grid;grid-template-columns:292px minmax(0,1fr);height:100vh}
.side{background:var(--surface);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:12px;padding:22px 20px 16px}
.brand h1{font:600 28px/1 var(--display);margin:0;letter-spacing:.01em}
.brand span{display:block;color:var(--muted);font-size:13px;margin-top:3px}
.new{margin:4px 16px 14px;justify-content:center}
.trips{overflow:auto;padding:2px 10px 10px;flex:1}
.sidefoot{border-top:1px solid var(--line);padding:12px 16px;display:flex;gap:8px;flex-wrap:wrap}
.sidefoot .btn{flex:1;justify-content:center;font-weight:500;padding:7px 10px;font-size:14px}

.btn{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:8px 14px;font-weight:600;text-decoration:none;transition:background .15s,border-color .15s}
.btn:hover{background:var(--raised);border-color:var(--muted)}
.btn.primary{background:var(--blue);border-color:var(--blue);color:var(--blue-ink)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{color:var(--red);border-color:var(--red)}
.btn.danger:hover{background:var(--red);color:#fff}
.btn.ghost{background:transparent;border-color:rgba(255,255,255,.4);color:#fff;font-weight:500;font-size:14px;padding:7px 12px}
.btn.ghost:hover{background:rgba(255,255,255,.14);border-color:#fff}
.linkbtn{background:none;border:0;padding:0;color:var(--blue);font-weight:600;text-decoration:underline;text-underline-offset:3px}
.iconbtn{border:0;background:none;padding:6px;border-radius:6px;color:var(--muted);display:grid;place-items:center}
.iconbtn:hover{background:var(--raised);color:var(--ink)}

.trip{display:flex;gap:14px;align-items:center;width:100%;text-align:left;background:none;border:1px solid transparent;border-radius:10px;padding:9px 10px;margin-bottom:2px}
.trip:hover{background:var(--raised)}
.trip[aria-current="true"]{background:var(--sky);border-color:var(--line)}
.stamp{flex:none;width:44px;height:52px;display:grid;place-items:center;color:#fff;font:600 17px var(--display);border-radius:3px;background:hsl(var(--h) 42% 36%);box-shadow:0 0 0 3px var(--surface),0 0 0 4px var(--line);transform:rotate(-3deg);margin:0 3px 0 2px}
.trip:nth-child(even) .stamp{transform:rotate(2.5deg)}
.tt{min-width:0;display:block}
.tn{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ts{display:block;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.main{overflow:auto;min-width:0}
.wrap{max-width:980px;margin:0 auto;padding:28px 32px 90px}
.hero{position:relative;background:var(--hero);color:#fff;border-radius:14px 14px 0 0;padding:30px 32px 28px;overflow:hidden}
.hero h2{font:600 clamp(34px,5vw,58px)/1.02 var(--display);margin:0 0 8px;letter-spacing:.005em;position:relative}
.hero .where{font-size:17px;opacity:.85;position:relative}
.hero .sum{margin:18px 0 0;max-width:46ch;font-size:16px;line-height:1.6;position:relative}
.hero .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:22px;position:relative}
.globe{position:absolute;right:-70px;top:-50px;width:360px;height:360px;opacity:.15;pointer-events:none}
.airmail{height:10px;border-radius:0 0 14px 14px;background:repeating-linear-gradient(135deg,#d62839 0 16px,#fff 16px 28px,#2f5fd0 28px 44px,#fff 44px 56px)}

.insights{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:36px;margin:28px 0 6px}
.insights h3{font:600 18px var(--display);margin:0 0 12px}
.insights svg{width:100%;height:auto;display:block}
.bar{fill:var(--blue)}
.bar:hover{fill:var(--red)}
.axis{fill:var(--muted);font:11px var(--text)}
.lieux{list-style:none;margin:0;padding:0}
.lieux li{display:grid;grid-template-columns:1fr auto;gap:2px 12px;margin-bottom:12px;align-items:baseline}
.lieux .linkbtn{text-align:left;text-decoration:none;color:var(--ink)}
.lieux .linkbtn:hover{color:var(--blue);text-decoration:underline}
.meter{grid-column:1/-1;height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.meter i{display:block;height:100%;background:var(--blue)}

.composer{background:var(--surface);border:1px solid var(--line);border-radius:12px;margin:24px 0 8px;padding:14px 16px}
.composer:focus-within{border-color:var(--blue)}
.composer textarea{width:100%;border:0;background:transparent;resize:none;outline:none;min-height:54px;font-size:16px;line-height:1.6;overflow:hidden}
.ebanner{background:var(--sky);border-radius:8px;padding:6px 10px;margin-bottom:8px;font-size:14px;display:flex;gap:12px;align-items:center}
.cfields{display:flex;flex-wrap:wrap;gap:10px;align-items:center;border-top:1px solid var(--line);padding-top:12px;margin-top:6px}
.field{display:block;border:1px solid var(--line);background:var(--raised);border-radius:8px;padding:6px 10px;min-width:0}
.field:focus-within{border-color:var(--blue)}
.field input{border:0;background:transparent;outline:none;width:100%;min-width:0;padding:1px 0}
.field.grow{flex:1 1 150px}
.field.w90{flex:0 0 110px}
.cfields .btn.primary{margin-left:auto}
.moods{display:flex;gap:2px}
.moods button{border:1px solid transparent;background:none;border-radius:8px;font-size:20px;line-height:1;padding:4px 5px;filter:grayscale(1);opacity:.55}
.moods button:hover{opacity:.9}
.moods button[aria-pressed="true"]{filter:none;opacity:1;background:var(--sky);border-color:var(--line)}

.tools{display:flex;gap:10px;align-items:center;margin:20px 0 4px;flex-wrap:wrap}
.search{flex:1 1 220px;display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 12px;color:var(--muted)}
.search:focus-within{border-color:var(--blue)}
.search input{flex:1;border:0;background:transparent;outline:none;color:var(--ink);min-width:0}
kbd{border:1px solid var(--line);border-radius:5px;padding:0 6px;font:12px var(--text);color:var(--muted)}
.tools .btn{font-weight:500;font-size:14px;padding:8px 12px}

.count,.none{color:var(--muted);margin:18px 2px}
.none.sm{margin:0}
.ticket{display:grid;grid-template-columns:136px minmax(0,1fr);background:var(--surface);border:1px solid var(--line);border-radius:10px;margin:20px 0;position:relative}
.stub{position:relative;padding:18px 14px 18px 20px;border-right:2px dashed var(--line)}
.stub .wd{color:var(--muted);text-transform:capitalize}
.stub .dn{font:600 48px/1 var(--display);display:block;margin:2px 0 4px}
.stub .my{font-size:14px;color:var(--muted);text-transform:capitalize}
.stub .tot{margin-top:16px;font-weight:600;font-size:14px;font-variant-numeric:tabular-nums}
.stub .pl{margin-top:2px;font-size:13px;color:var(--muted)}
.notch{position:absolute;right:-10px;width:18px;height:18px;border-radius:50%;background:var(--bg);border:1px solid transparent;transform:rotate(45deg)}
.notch.t{top:-10px;border-bottom-color:var(--line);border-right-color:var(--line)}
.notch.b{bottom:-10px;border-top-color:var(--line);border-left-color:var(--line)}
.entries{padding:2px 22px;min-width:0}
.entry{padding:16px 0;border-top:1px solid var(--line)}
.entry:first-child{border-top:0}
.ehead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px;min-height:34px}
.eplace{font:600 18px var(--display)}
.emood{font-size:18px}
.ecost{margin-left:auto;font-weight:600;font-variant-numeric:tabular-nums}
.eact{display:flex;gap:2px;opacity:0;transition:opacity .15s}
.entry:hover .eact,.entry:focus-within .eact{opacity:1}
@media (hover:none){.eact{opacity:1}}
.etext{margin:0;white-space:pre-wrap;line-height:1.65;max-width:66ch;overflow-wrap:anywhere}
.etags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tag{border:1px solid var(--line);border-radius:999px;background:none;padding:1px 10px;font-size:13px;color:var(--muted)}
.tag:hover{border-color:var(--blue);color:var(--blue)}

.empty{max-width:480px;margin:16vh auto 0;padding:0 24px;text-align:center}
.empty h2{font:600 36px/1.1 var(--display);margin:18px 0 8px}
.empty p{color:var(--muted);margin:0 0 22px}

.scrim{position:fixed;inset:0;background:rgba(8,14,30,.55);backdrop-filter:blur(3px);display:grid;place-items:center;padding:20px;z-index:20;animation:fade .15s}
.dialog{width:min(470px,100%);background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:24px;animation:pop .18s}
.dialog h3{font:600 26px/1.1 var(--display);margin:0 0 6px}
.dialog p{margin:6px 0 0;color:var(--muted)}
.dialog label{display:block;margin:14px 0 5px;font-weight:600;font-size:14px}
.dialog input{width:100%;border:1px solid var(--line);background:var(--raised);border-radius:8px;padding:9px 10px;outline:none}
.dialog input:focus{border-color:var(--blue)}
.dgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.dactions{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}
#toasts{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:30;align-items:center;pointer-events:none}
.toast{pointer-events:auto;background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;display:flex;gap:16px;align-items:center;animation:pop .18s}
.toast button{background:none;border:0;color:inherit;font-weight:700;text-decoration:underline}
.photos{border-top:1px solid var(--line);padding:14px 0 18px}
.entries>.entry:first-child+.photos,.entries>.nonote+.photos{margin-top:0}
.pgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.ph{position:relative;aspect-ratio:4/3;border-radius:8px;overflow:hidden;background:var(--raised);border:1px solid var(--line)}
.ph .open{position:absolute;inset:0;border:0;padding:0;background:none;cursor:zoom-in}
.ph img{width:100%;height:100%;object-fit:cover;display:block}
.ph .pdel{position:absolute;top:6px;right:6px;width:28px;height:28px;border:0;border-radius:999px;background:rgba(10,16,32,.72);color:#fff;display:grid;place-items:center;opacity:0;transition:opacity .15s}
.ph:hover .pdel,.ph:focus-within .pdel{opacity:1}
.ph .pdel:hover{background:var(--red)}
@media (hover:none){.ph .pdel{opacity:1}}
.padd,.pbar{border:2px dashed var(--line);border-radius:8px;background:none;color:var(--muted);transition:border-color .15s,color .15s}
.padd{aspect-ratio:4/3;display:grid;place-content:center;justify-items:center;gap:2px;font-size:14px}
.pbar{width:100%;padding:10px;display:flex;gap:8px;justify-content:center;align-items:center}
.padd:hover,.pbar:hover{border-color:var(--blue);color:var(--blue)}
.nonote{color:var(--muted);padding:16px 0 4px;margin:0}
.drop{outline:2px dashed var(--blue);outline-offset:3px}
.lb{position:fixed;inset:0;z-index:40;background:rgba(6,10,22,.97);display:grid;place-items:center;padding:24px;animation:fade .15s}
.lb figure{margin:0;max-width:min(1100px,94vw);display:grid;gap:10px;justify-items:center}
.lb img{max-width:100%;max-height:82vh;border-radius:8px;display:block;object-fit:contain}
.lb figcaption{color:#cdd6ea;font-size:14px}
.lbx,.lbn{position:absolute;border:0;background:rgba(255,255,255,.12);color:#fff;border-radius:999px;width:46px;height:46px;display:grid;place-items:center}
.lbx{top:18px;right:18px}.lbn{top:50%;transform:translateY(-50%)}.lbn.prev{left:18px}.lbn.next{right:18px}
.lbx:hover,.lbn:hover{background:rgba(255,255,255,.25)}
.favbtn{border:0;background:none;padding:2px 5px;font-size:21px;line-height:1;color:var(--muted);opacity:0;transition:opacity .15s,color .15s,transform .15s;border-radius:6px}
.entry:hover .favbtn,.favbtn:focus-visible{opacity:.75}
.favbtn:hover{color:var(--gold);opacity:1;transform:scale(1.12)}
.favbtn[aria-pressed="true"]{opacity:1;color:var(--gold)}
@media (hover:none){.favbtn{opacity:.6}.favbtn[aria-pressed="true"]{opacity:1}}
.cfields .favbtn{opacity:1;font-size:24px}
.stub .starday{color:var(--gold);font-size:18px;margin-left:6px;vertical-align:.25em}
.btn[aria-pressed="true"]{background:var(--sky);border-color:var(--blue)}
.hero .budget{position:relative;margin-top:18px;max-width:540px}
.bbar{height:8px;border-radius:5px;background:rgba(255,255,255,.22);overflow:hidden}
.bbar i{display:block;height:100%;background:#fff;border-radius:5px}
.budget.over .bbar i{background:#ff7b8a}
.hero .budget p{margin:8px 0 0;font-size:15px}
.hero.has-cover{background:linear-gradient(180deg,rgba(14,26,64,.55) 0%,rgba(14,26,64,.92) 100%),var(--cover) center/cover no-repeat}
.hero.has-cover .globe{opacity:.07}
.stamp.photo{background-size:cover;background-position:center;color:transparent}
.ph .pcover{position:absolute;top:6px;left:6px;width:28px;height:28px;border:0;border-radius:999px;background:rgba(10,16,32,.72);color:#fff;display:grid;place-items:center;opacity:0;transition:opacity .15s}
.ph:hover .pcover,.ph:focus-within .pcover{opacity:1}
.ph .pcover:hover{background:var(--blue)}
.ph .badge{position:absolute;left:6px;bottom:6px;background:rgba(10,16,32,.8);color:#fff;font-size:12px;padding:2px 8px;border-radius:999px;pointer-events:none}
@media (hover:none){.ph .pcover{opacity:1}}
.frise{margin:8px 0 0}
.frise h3{font:600 18px var(--display);margin:0 0 10px}
.fgrid{display:flex;flex-wrap:wrap;gap:6px}
.fm{flex-basis:100%;font-size:13px;color:var(--muted);margin:6px 0 0;text-transform:capitalize}
.fday{position:relative;width:32px;height:32px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums;display:grid;place-items:center;padding:0}
.fday:hover{border-color:var(--blue);color:var(--ink)}
.fday.has{background:var(--blue);border-color:var(--blue);color:var(--blue-ink);font-weight:600}
.fday.hasph::after{content:"";position:absolute;right:3px;bottom:3px;width:6px;height:6px;border-radius:50%;background:var(--red)}
.fday.hasfav{box-shadow:0 0 0 2px var(--gold)}
.fday.today{outline:2px dotted var(--ink);outline-offset:2px}
.flegend{margin:10px 0 0;font-size:13px;color:var(--muted)}
.ticket.flash{animation:flash 1.4s}
@keyframes flash{0%{box-shadow:0 0 0 4px var(--blue)}100%{box-shadow:0 0 0 4px transparent}}
.sidefoot .wide{flex:1 1 100%}
:root{--gold:#d99a00}
:root[data-theme="dark"]{--gold:#f2c14e}
@media (prefers-color-scheme:dark){:root[data-theme="auto"]{--gold:#f2c14e}}
@media print{
  :root,:root[data-theme="dark"],:root[data-theme="auto"],:root[data-theme="light"]{--bg:#fff;--surface:#fff;--raised:#f6f6f6;--ink:#111;--muted:#555;--line:#cfcfcf;--blue:#2447b5;--blue-ink:#fff;--sky:#eef2fb;--gold:#b97a00}
  @page{margin:14mm}
  html,body{height:auto;background:#fff}
  .app{display:block;height:auto}
  .side,.tools,.composer,.hero .acts,.eact,.padd,.pbar,.pdel,.pcover,.ph .badge,.favbtn:not([aria-pressed="true"]),.frise,#toasts,.scrim,.lb{display:none!important}
  .main{overflow:visible}
  .wrap{max-width:none;padding:0}
  .hero,.airmail,.bbar,.bbar i,.meter i,.bar,.fday,.stamp{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .hero{border-radius:0}.airmail{border-radius:0}
  .ticket{break-inside:avoid;margin:14px 0}
  .insights{break-inside:avoid}
}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes pop{from{opacity:0;transform:translateY(6px) scale(.98)}to{opacity:1;transform:none}}

@media screen and (max-width:860px){
  .app{grid-template-columns:minmax(0,1fr);height:auto;min-height:100vh}
  .side{min-width:0}
  .side{border-right:0;border-bottom:1px solid var(--line)}
  .trips{display:flex;gap:8px;overflow-x:auto;flex:none}
  .trip{min-width:210px;width:auto;border-color:var(--line)}
  .main{overflow:visible}
  .wrap{padding:18px 16px 70px}
  .insights{grid-template-columns:minmax(0,1fr);gap:24px}
  .ticket{grid-template-columns:minmax(0,1fr)}
  .stub{border-right:0;border-bottom:2px dashed var(--line);display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;padding:14px 18px}
  .stub .dn{font-size:34px;margin:0}
  .stub .tot{margin:0}
  .notch{display:none}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <svg viewBox="0 0 40 40" width="38" height="38" aria-hidden="true"><rect width="40" height="40" rx="9" style="fill:var(--hero)"/><circle cx="20" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2"/><path d="M20 10l4.5 10-4.5 10-4.5-10z" fill="#d62839"/></svg>
      <div><h1>Carnet</h1><span>journal de voyage</span></div>
    </div>
    <button class="btn primary new" data-act="new-voyage" id="b-new"></button>
    <nav class="trips" id="trips" aria-label="Voyages"></nav>
    <div class="sidefoot">
      <a class="btn wide" href="/api/sauvegarde" download id="b-save" title="Télécharge une copie complète de ton carnet (notes, photos, réglages)"></a>
      <button class="btn" data-act="theme" id="b-theme"></button>
      <button class="btn" data-act="quit" id="b-quit"></button>
    </div>
  </aside>
  <main class="main" id="main"></main>
</div>
<div id="toasts" aria-live="polite"></div>
<input type="file" id="file" accept="image/*" multiple hidden>

<script>
(()=>{
'use strict';
const $=(s,r=document)=>r.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const EUR=new Intl.NumberFormat('fr-FR',{style:'currency',currency:'EUR'});
const NUM1=new Intl.NumberFormat('fr-FR',{maximumFractionDigits:1});
const MOODS=['','😞','😕','😐','🙂','🤩'];
const MOODTXT=['','Difficile','Bof','Correct','Bien','Génial'];
const PATHS={
  edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
  trash:'<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  theme:'<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 010 18z" fill="currentColor"/>',
  down:'<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M5 21h14"/>',
  power:'<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1011.4 0"/>',
  sort:'<path d="M7 4v16M7 20l-3-3M7 20l3-3"/><path d="M17 20V4M17 4l-3 3M17 4l3 3"/>',
  image:'<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/>',
  close:'<path d="M6 6l12 12M18 6L6 18"/>',
  left:'<path d="M15 5l-7 7 7 7"/>',
  right:'<path d="M9 5l7 7-7 7"/>',
  flag:'<path d="M5 21V4"/><path d="M5 4h12l-2 4 2 4H5"/>',
  print:'<path d="M6 9V3h12v6"/><rect x="4" y="9" width="16" height="9" rx="2"/><path d="M7 14h10v7H7z"/>',
  disk:'<path d="M5 3h11l4 4v14H5z"/><path d="M8 3v6h8V3"/><rect x="8" y="14" width="8" height="7"/>'
};
const ic=(n,s=18)=>`<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[n]}</svg>`;
const GLOBE='<svg class="globe" viewBox="0 0 200 200" fill="none" stroke="#fff" stroke-width="1.2" aria-hidden="true"><circle cx="100" cy="100" r="90"/><ellipse cx="100" cy="100" rx="40" ry="90"/><ellipse cx="100" cy="100" rx="75" ry="90"/><path d="M10 100h180M20 60h160M20 140h160"/></svg>';
const COMPASS='<svg viewBox="0 0 40 40" width="84" height="84" aria-hidden="true"><rect width="40" height="40" rx="9" style="fill:var(--hero)"/><circle cx="20" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2"/><path d="M20 10l4.5 10-4.5 10-4.5-10z" fill="#d62839"/></svg>';

const S={voyages:[],vid:null,d:null,q:'',edit:null,theme:'auto',desc:false,mood:null,fav:false,favOnly:false};
const THEMES=['auto','clair','sombre'];
const THEMELBL={auto:'Thème auto',clair:'Thème clair',sombre:'Thème sombre'};

async function api(m,u,b){
  const o={method:m,headers:{}};
  if(m==='POST'||m==='PUT'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(b||{});}
  const r=await fetch('/api/'+u,o);
  let j={};try{j=await r.json()}catch(e){}
  if(!r.ok)throw new Error(j.erreur||('Erreur '+r.status));
  return j;
}
function toast(msg,action,cb){
  const pile=$('#toasts').children;while(pile.length>=2)pile[0].remove();
  const el=document.createElement('div');el.className='toast';el.setAttribute('role','status');
  el.innerHTML=`<span>${esc(msg)}</span>`;
  if(action){const b=document.createElement('button');b.textContent=action;b.onclick=()=>{el.remove();cb()};el.append(b)}
  $('#toasts').append(el);setTimeout(()=>el.remove(),action?7000:3500);
}
const fail=e=>toast(e&&e.message?e.message:'Une erreur est survenue.');

const hue=s=>{let h=0;for(const c of s)h=(h*31+c.codePointAt(0))%360;return h};
const initials=s=>[...s.trim()].slice(0,2).join('').toUpperCase();
const pl=(n,s,p)=>`${n} ${n>1?(p||s+'s'):s}`;
const D=j=>new Date(j+'T12:00:00');
const today=()=>new Date().toLocaleDateString('sv-SE');
const fCourt=j=>D(j).toLocaleDateString('fr-FR',{day:'numeric',month:'short',year:'numeric'}).replace(/^1 /,'1er ');
function range(v){
  if(v.debut&&v.fin)return `du ${fCourt(v.debut)} au ${fCourt(v.fin)}`;
  if(v.debut)return `à partir du ${fCourt(v.debut)}`;
  if(v.fin)return `jusqu’au ${fCourt(v.fin)}`;
  return '';
}
const where=v=>[v.pays,range(v)].filter(Boolean).join(', ');

function applyTheme(){
  document.documentElement.dataset.theme=S.theme==='clair'?'light':S.theme==='sombre'?'dark':'auto';
  $('#b-theme').innerHTML=ic('theme')+THEMELBL[S.theme];
}

/* ---------- barre latérale ---------- */
function renderSide(){
  $('#trips').innerHTML=S.voyages.map(v=>`<button class="trip" data-vid="${v.id}" aria-current="${v.id===S.vid}">
    <span class="stamp${v.couverture?' photo':''}" style="--h:${hue(v.nom)}${v.couverture?`;background-image:url(/api/photo/${v.couverture}?v=mini)`:''}">${v.couverture?'':esc(initials(v.nom))}</span>
    <span class="tt"><span class="tn">${esc(v.nom)}</span><span class="ts">${esc(v.pays||'Sans pays')}</span><span class="ts">${pl(v.nb,'note')}${v.total?', '+EUR.format(v.total):''}</span></span></button>`).join('')
    ||'<p class="ts" style="padding:8px 12px;white-space:normal">Aucun voyage pour l’instant.</p>';
}

/* ---------- zone principale ---------- */
function heroActs(){
  const v=S.d.voyage;
  return `<button class="btn ghost" data-act="edit-voyage">${ic('edit',16)}Modifier</button>
    <a class="btn ghost" href="/api/voyage/${v.id}/export?format=md" download>${ic('down',16)}Exporter en Markdown</a>
    ${S.d.photos.length?`<a class="btn ghost" href="/api/voyage/${v.id}/export?format=zip" download>${ic('down',16)}Exporter avec les photos</a>`:''}
    <a class="btn ghost" href="/api/voyage/${v.id}/export?format=json" download>${ic('down',16)}Exporter en JSON</a>
    <button class="btn ghost" data-act="print">${ic('print',16)}Imprimer / PDF</button>
    <button class="btn ghost" data-act="del-voyage">${ic('trash',16)}Supprimer le voyage</button>`;
}
function budgetHtml(){
  const v=S.d.voyage,s=S.d.stats;
  if(v.budget==null)return '';
  const pct=v.budget>0?s.total/v.budget:0,over=s.total>v.budget,jours=s.par_jour.length;
  const reste=over?`dépassé de ${EUR.format(s.total-v.budget)}`:`il reste ${EUR.format(v.budget-s.total)}`;
  return `<div class="budget${over?' over':''}"><div class="bbar" role="progressbar" aria-label="Budget utilisé" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(100,Math.round(pct*100))}"><i style="width:${Math.min(100,pct*100).toFixed(1)}%"></i></div><p>${EUR.format(s.total)} dépensés sur ${EUR.format(v.budget)} (${Math.round(pct*100)} %), ${reste}.${jours&&s.total?` Moyenne : ${EUR.format(s.total/jours)} par jour noté.`:''}</p></div>`;
}
function heroHtml(){
  const v=S.d.voyage;
  return `<div class="hero${v.couverture?' has-cover':''}"${v.couverture?` style="--cover:url(/api/photo/${v.couverture})"`:''}>${GLOBE}
    <h2>${esc(v.nom)}</h2>
    <div class="where">${esc(where(v))}</div>
    <p class="sum" id="sum">${esc(summary())}</p>
    ${budgetHtml()}
    <div class="acts" id="acts">${heroActs()}</div>
  </div>
  <div class="airmail"></div>`;
}
function renderHero(){const h=$('#hero');if(h)h.innerHTML=heroHtml()}
function renderMain(){
  const m=$('#main');
  if(!S.voyages.length||!S.d){
    m.innerHTML=`<div class="empty">${COMPASS}<h2>Aucun voyage pour l’instant</h2><p>Crée un voyage, puis note tes journées : lieux, humeur, dépenses.</p><button class="btn primary" data-act="new-voyage">${ic('plus')}Créer un voyage</button></div>`;
    document.title='Carnet de voyage';return;
  }
  const v=S.d.voyage;
  document.title=v.nom+' – Carnet';
  m.innerHTML=`<div class="wrap">
  <header id="hero">${heroHtml()}</header>
  <section class="insights" id="insights"></section>
  <section class="frise" id="frise"></section>
  <form class="composer" id="composer" autocomplete="off">
    <div class="ebanner" id="ebanner" hidden><span>Modification d’une note</span><button type="button" class="linkbtn" data-act="cancel-edit">Annuler</button></div>
    <textarea id="f-texte" rows="2" placeholder="Qu’est-ce qui s’est passé aujourd’hui ?" required></textarea>
    <div class="cfields">
      <label class="field"><span class="sr">Jour</span><input type="date" id="f-jour" required></label>
      <label class="field grow"><span class="sr">Lieu</span><input id="f-lieu" list="lieux" placeholder="Lieu" maxlength="120"><datalist id="lieux"></datalist></label>
      <div class="moods" id="f-moods" role="group" aria-label="Humeur">${[1,2,3,4,5].map(i=>`<button type="button" data-mood="${i}" aria-pressed="false" title="${MOODTXT[i]}" aria-label="${MOODTXT[i]}">${MOODS[i]}</button>`).join('')}</div>
      <button type="button" class="favbtn" id="f-fav" data-act="composer-fav" aria-pressed="false" aria-label="Moment fort" title="Marquer comme moment fort">☆</button>
      <label class="field w90"><span class="sr">Dépense en euros</span><input id="f-dep" inputmode="decimal" placeholder="Dépense €"></label>
      <label class="field grow"><span class="sr">Tags</span><input id="f-tags" placeholder="Tags : food, temple"></label>
      <button type="button" class="btn" data-act="photo-composer" title="Ajouter des photos à la date choisie">${ic('image',16)}Photos</button>
      <button class="btn primary" id="f-submit" title="Ctrl + Entrée">Ajouter la note</button>
    </div>
  </form>
  <div class="tools">
    <label class="search">${ic('search',18)}<span class="sr">Rechercher</span><input id="q" type="search" placeholder="Rechercher dans ce voyage" value="${esc(S.q)}"><kbd>/</kbd></label>
    <button class="btn" data-act="fav-filter" id="b-fav" aria-pressed="false"></button>
    <button class="btn" data-act="order" id="b-order">${ic('sort',16)}<span></span></button>
  </div>
  <section id="timeline"></section>
  </div>`;
  renderInsights();renderFrise();renderTimeline();updateLieux();setComposer(null);paintTools();
}

function summary(){
  const s=S.d.stats,ph=S.d.photos.length,avecBudget=S.d.voyage.budget!=null;
  if(!s.nb)return ph?`Aucune note pour l’instant, ${pl(ph,'photo')}.`:'Ce carnet est encore vide.';
  let t=`${pl(s.nb,'note')} écrite${s.nb>1?'s':''}`+(s.lieux?` dans ${pl(s.lieux,'lieu','lieux')}`:'')+'.';
  if(s.total&&!avecBudget)t+=` ${EUR.format(s.total)} dépensés`+(s.humeur?`, humeur moyenne de ${NUM1.format(s.humeur)} sur 5.`:'.');
  else if(s.humeur)t+=` Humeur moyenne de ${NUM1.format(s.humeur)} sur 5.`;
  if(s.favoris)t+=` ${pl(s.favoris,'moment fort','moments forts')}.`;
  if(ph)t+=` ${pl(ph,'photo')}.`;
  return t;
}
function chart(pj){
  if(!pj.some(d=>d.total>0))return '<p class="none sm">Aucune dépense enregistrée pour ce voyage.</p>';
  const W=600,H=170,B=24,T=8,n=pj.length,max=Math.max(...pj.map(d=>d.total)),slot=(W-8)/n,bw=Math.max(3,Math.min(34,slot-6)),step=Math.ceil(n/12);
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Dépenses par jour"><line x1="0" x2="${W}" y1="${H-B}" y2="${H-B}" stroke="var(--line)"/>`;
  pj.forEach((d,i)=>{
    const h=Math.max(d.total>0?3:0,(d.total/max)*(H-B-T-14)),x=4+i*slot+(slot-bw)/2,y=H-B-h;
    s+=`<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="3"><title>${esc(fCourt(d.jour))} : ${EUR.format(d.total)}</title></rect>`;
    if(i%step===0)s+=`<text class="axis" x="${(x+bw/2).toFixed(1)}" y="${H-7}" text-anchor="middle">${D(d.jour).getDate()}</text>`;
  });
  return s+`<text class="axis" x="4" y="12">${EUR.format(max)}</text></svg>`;
}
function renderInsights(){
  const s=S.d.stats,el=$('#insights');
  if(!s.nb){el.hidden=true;return}
  el.hidden=false;
  const max=Math.max(...s.top_lieux.map(l=>l.nb),1);
  el.innerHTML=`<div><h3>Dépenses par jour</h3>${chart(s.par_jour)}</div>`+
    (s.top_lieux.length?`<div><h3>Lieux les plus visités</h3><ul class="lieux">${s.top_lieux.map(l=>`<li><button class="linkbtn" data-q="${esc(l.lieu)}">${esc(l.lieu)}</button><span class="ts">${pl(l.nb,'note')}</span><div class="meter"><i style="width:${(l.nb/max*100).toFixed(0)}%"></i></div></li>`).join('')}</ul></div>`:'');
}
function dateParDefaut(){
  const v=S.d&&S.d.voyage,t=today();
  if(!v)return t;
  if(v.debut&&t<v.debut)return v.debut;
  if(v.fin&&t>v.fin)return v.fin;
  return t;
}
function renderFrise(){
  const el=$('#frise');if(!el)return;
  const v=S.d.voyage,info={};
  S.d.stats.par_jour.forEach(d=>{info[d.jour]={nb:d.nb,fav:d.favoris,ph:0}});
  S.d.photos.forEach(p=>{(info[p.jour]??={nb:0,fav:0,ph:0}).ph++});
  const jours=Object.keys(info).sort();
  let a=v.debut||jours[0],b=v.fin||jours[jours.length-1];
  if(!a||!b){el.hidden=true;return}
  if(jours.length){if(jours[0]<a)a=jours[0];if(jours[jours.length-1]>b)b=jours[jours.length-1]}
  const n=Math.round((D(b)-D(a))/864e5)+1;
  if(n<1||n>120){el.hidden=true;return}
  el.hidden=false;
  let html='',mois='';const t=today();
  for(let i=0;i<n;i++){
    const d=new Date(D(a).getTime()+i*864e5),j=d.toLocaleDateString('sv-SE'),m=d.toLocaleDateString('fr-FR',{month:'long',year:'numeric'});
    if(m!==mois){mois=m;html+=`<span class="fm">${esc(m)}</span>`}
    const x=info[j]||{nb:0,fav:0,ph:0};
    const tip=`${fCourt(j)} : ${x.nb?pl(x.nb,'note'):'aucune note'}${x.ph?', '+pl(x.ph,'photo'):''}${x.fav?', moment fort':''}`;
    html+=`<button class="fday${x.nb?' has':''}${x.ph?' hasph':''}${x.fav?' hasfav':''}${j===t?' today':''}" data-act="goto-day" data-day="${j}" title="${esc(tip)}" aria-label="${esc(tip)}">${d.getDate()}</button>`;
  }
  el.innerHTML=`<h3>Les jours du voyage</h3><div class="fgrid">${html}</div><p class="flegend">Un jour plein a une note. Un point rouge indique des photos, un contour doré un moment fort. Un clic ouvre le jour ou prépare la saisie.</p>`;
}
function updateLieux(){$('#lieux').innerHTML=S.d.lieux.map(l=>`<option value="${esc(l)}">`).join('')}

const MAXP=3;
const photosDe=j=>S.d.photos.filter(p=>p.jour===j);
function entryHtml(n){
  return `<div class="entry" data-id="${n.id}">
    <div class="ehead">${n.lieu?`<span class="eplace">${esc(n.lieu)}</span>`:''}${n.humeur?`<span class="emood" title="${MOODTXT[n.humeur]}">${MOODS[n.humeur]}</span>`:''}
      <button class="favbtn" data-act="fav" aria-pressed="${n.favori?'true':'false'}" aria-label="${n.favori?'Retirer des moments forts':'Marquer comme moment fort'}" title="Moment fort">${n.favori?'★':'☆'}</button>
      <span class="ecost">${n.depense?EUR.format(n.depense):''}</span>
      <span class="eact"><button class="iconbtn" data-act="edit" aria-label="Modifier la note">${ic('edit')}</button><button class="iconbtn" data-act="del" aria-label="Supprimer la note">${ic('trash')}</button></span></div>
    <p class="etext">${esc(n.texte)}</p>
    ${n.tags?`<div class="etags">${n.tags.split(',').map(t=>`<button class="tag" data-q="${esc(t)}">#${esc(t)}</button>`).join('')}</div>`:''}
  </div>`;
}
function photoBlock(j){
  const ps=photosDe(j),cov=S.d.voyage.couverture;
  if(!ps.length)return `<div class="photos"><button class="pbar" data-act="photo-add">${ic('image',18)}Ajouter des photos (${MAXP} maximum par jour)</button></div>`;
  return `<div class="photos"><div class="pgrid">${ps.map((p,i)=>`<div class="ph"><button class="open" data-act="photo-open" data-pid="${p.id}" aria-label="Agrandir la photo ${i+1}"><img src="/api/photo/${p.id}?v=mini" alt="Photo ${i+1} du ${esc(fCourt(j))}" loading="lazy" decoding="async"></button><button class="pcover" data-act="photo-cover" data-pid="${p.id}" aria-label="${p.id===cov?'Retirer la couverture':'Utiliser comme couverture'}" title="${p.id===cov?'Retirer la couverture':'Utiliser comme couverture'}">${ic('flag',16)}</button>${p.id===cov?'<span class="badge">Couverture</span>':''}<button class="pdel" data-act="photo-del" data-pid="${p.id}" aria-label="Supprimer la photo ${i+1}">${ic('close',16)}</button></div>`).join('')}${ps.length<MAXP?`<button class="padd" data-act="photo-add">${ic('image',20)}<span>Ajouter une photo</span><span class="ts">${ps.length} sur ${MAXP}</span></button>`:''}</div></div>`;
}
function ticket(j,ns){
  const tot=ns.reduce((a,n)=>a+n.depense,0),lieux=[...new Set(ns.map(n=>n.lieu).filter(Boolean))];
  const dt=D(j);
  return `<article class="ticket" data-jour="${j}"><div class="stub"><span class="notch t"></span><span class="notch b"></span>
    <div class="wd">${dt.toLocaleDateString('fr-FR',{weekday:'long'})}</div>
    <span class="dn">${dt.getDate()}${ns.some(n=>n.favori)?'<span class="starday" title="Moment fort">★</span>':''}</span>
    <div class="my">${dt.toLocaleDateString('fr-FR',{month:'long',year:'numeric'})}</div>
    ${tot?`<div class="tot">${EUR.format(tot)}</div>`:''}
    ${lieux.length?`<div class="pl">${esc(lieux.slice(0,3).join(', '))}</div>`:''}
  </div><div class="entries">${ns.length?ns.map(entryHtml).join(''):'<p class="nonote">Aucune note ce jour-là.</p>'}${photoBlock(j)}</div></article>`;
}
function renderTimeline(){
  const el=$('#timeline'),g={};
  const notes=S.favOnly?S.d.notes.filter(n=>n.favori):S.d.notes;
  notes.forEach(n=>(g[n.jour]??=[]).push(n));
  if(!S.q&&!S.favOnly)S.d.photos.forEach(p=>{g[p.jour]??=[]});
  const jours=Object.keys(g).sort();if(S.desc)jours.reverse();
  if(!jours.length){
    el.innerHTML=S.q?`<p class="none">Aucun résultat pour « ${esc(S.q)} ».</p>`:S.favOnly?'<p class="none">Aucun moment fort pour l’instant. Clique sur l’étoile d’une note pour la garder ici.</p>':'<p class="none">Aucune note pour l’instant. Écris la première ci-dessus.</p>';
    return;
  }
  const entete=S.q?`<p class="count">${pl(notes.length,'résultat')} pour « ${esc(S.q)} »</p>`:S.favOnly?`<p class="count">${pl(notes.length,'moment fort','moments forts')}</p>`:'';
  el.innerHTML=entete+jours.map(j=>ticket(j,g[j])).join('');
}
function paintTools(){
  const b=$('#b-order span');if(b)b.textContent=S.desc?'Plus récent d’abord':'Plus ancien d’abord';
  const f=$('#b-fav');
  if(f){const n=S.d?S.d.stats.favoris:0;f.setAttribute('aria-pressed',String(S.favOnly));f.innerHTML=`<span aria-hidden="true">${S.favOnly?'★':'☆'}</span> Moments forts${n?` (${n})`:''}`}
}

/* ---------- saisie ---------- */
function paintMood(){document.querySelectorAll('#f-moods button').forEach(b=>b.setAttribute('aria-pressed',String(+b.dataset.mood===S.mood)))}
function paintFav(){const b=$('#f-fav');if(b){b.setAttribute('aria-pressed',String(S.fav));b.textContent=S.fav?'★':'☆'}}
function autosize(){const t=$('#f-texte');if(!t)return;t.style.height='auto';t.style.height=Math.max(54,t.scrollHeight)+'px'}
function setComposer(n){
  S.edit=n||null;
  $('#ebanner').hidden=!n;
  $('#f-submit').textContent=n?'Enregistrer':'Ajouter la note';
  if(n){
    $('#f-texte').value=n.texte;$('#f-jour').value=n.jour;$('#f-lieu').value=n.lieu||'';S.mood=n.humeur;S.fav=!!n.favori;
    $('#f-dep').value=n.depense?String(n.depense).replace('.',','):'';$('#f-tags').value=n.tags?n.tags.split(',').join(', '):'';
  }else{
    $('#f-texte').value='';$('#f-dep').value='';$('#f-tags').value='';S.mood=null;S.fav=false;
    if(!$('#f-jour').value)$('#f-jour').value=dateParDefaut();
  }
  paintMood();paintFav();autosize();
}

/* ---------- données ---------- */
const enc=encodeURIComponent;
async function refresh(){
  const [d,v]=await Promise.all([api('GET',`voyage/${S.vid}?q=${enc(S.q)}`),api('GET','voyages')]);
  S.d=d;S.voyages=v.voyages;
  renderSide();renderHero();renderInsights();renderFrise();renderTimeline();updateLieux();paintTools();
}
async function select(vid){
  S.vid=vid;S.q='';S.edit=null;
  try{
    S.d=vid?await api('GET',`voyage/${vid}`):null;
    api('PUT','reglage',{cle:'dernier',valeur:String(vid||'')}).catch(()=>{});
    renderSide();renderMain();
  }catch(e){fail(e)}
}
async function reloadVoyages(){S.voyages=(await api('GET','voyages')).voyages}

function b64(blob){return new Promise((ok,ko)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.onerror=ko;r.readAsDataURL(blob)})}
function jpeg(bmp,max,q){
  const r=Math.min(1,max/Math.max(bmp.width,bmp.height)),w=Math.max(1,Math.round(bmp.width*r)),h=Math.max(1,Math.round(bmp.height*r));
  const c=document.createElement('canvas');c.width=w;c.height=h;
  const x=c.getContext('2d');x.fillStyle='#fff';x.fillRect(0,0,w,h);x.drawImage(bmp,0,0,w,h);
  return new Promise(ok=>c.toBlob(ok,'image/jpeg',q));
}
async function ajouterPhotos(files,jour){
  if(!S.vid)return;
  const imgs=[...files].filter(f=>f.type.startsWith('image/'));
  if(!imgs.length){toast('Choisis un fichier image.');return}
  const libres=MAXP-photosDe(jour).length;
  if(libres<=0){toast(`Cette journée a déjà ${MAXP} photos.`);return}
  const prises=imgs.slice(0,libres);let ok=0;
  try{
    for(const f of prises){
      let bmp;
      try{bmp=await createImageBitmap(f,{imageOrientation:'from-image'})}catch(e){throw new Error('Format d’image non pris en charge.')}
      const [g,m]=await Promise.all([jpeg(bmp,1600,.85),jpeg(bmp,480,.8)]);
      if(bmp.close)bmp.close();
      await api('POST','photo',{voyage_id:S.vid,jour,grande:await b64(g),mini:await b64(m)});ok++;
    }
  }catch(e){fail(e)}
  if(ok){
    await refresh();
    const s=`${pl(ok,'photo')} ajoutée${ok>1?'s':''}`;
    toast(imgs.length>prises.length?`${s}. ${MAXP} photos maximum par jour.`:s+'.');
  }
}
function lightbox(pid){
  const ph=S.d.photos.find(p=>p.id===pid);if(!ph)return;
  const j=ph.jour,liste=photosDe(j);let i=liste.findIndex(p=>p.id===pid);
  const el=document.createElement('div');el.className='lb';el.setAttribute('role','dialog');el.setAttribute('aria-label','Photo agrandie');
  const draw=()=>{el.innerHTML=`<button class="lbx" data-lb="close" aria-label="Fermer">${ic('close',22)}</button>`+
    (liste.length>1?`<button class="lbn prev" data-lb="prev" aria-label="Photo précédente">${ic('left',26)}</button><button class="lbn next" data-lb="next" aria-label="Photo suivante">${ic('right',26)}</button>`:'')+
    `<figure><img src="/api/photo/${liste[i].id}" alt="Photo ${i+1} du ${esc(fCourt(j))}"><figcaption>${esc(fCourt(j))}, photo ${i+1} sur ${liste.length}</figcaption></figure>`};
  const go=d=>{i=(i+d+liste.length)%liste.length;draw()};
  const close=()=>{el.remove();document.removeEventListener('keydown',key,true)};
  const key=e=>{
    if(e.key==='Escape'){e.stopPropagation();close()}
    else if(e.key==='ArrowLeft'&&liste.length>1)go(-1);
    else if(e.key==='ArrowRight'&&liste.length>1)go(1);
  };
  el.addEventListener('click',e=>{
    const a=e.target.closest('[data-lb]');const k=a?a.dataset.lb:null;
    if(k==='prev')go(-1);else if(k==='next')go(1);else if(k==='close'||e.target===el||e.target.tagName==='FIGURE')close();
  });
  document.addEventListener('keydown',key,true);draw();document.body.append(el);el.querySelector('.lbx').focus();
}
function photoDelDialog(pid){
  dialog(`<h3>Supprimer cette photo ?</h3><p>Cette action est définitive.</p>
    <div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn danger">Supprimer la photo</button></div>`,
  async(f,close)=>{await api('DELETE','photo/'+pid);close();await refresh();toast('Photo supprimée.')});
}

function dialog(html,onsubmit){
  const s=document.createElement('div');s.className='scrim';
  s.innerHTML=`<form class="dialog" role="dialog" aria-modal="true">${html}</form>`;
  document.body.append(s);
  const f=s.firstElementChild,close=()=>s.remove();
  s.addEventListener('mousedown',e=>{if(e.target===s)close()});
  f.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',close));
  f.addEventListener('submit',async ev=>{ev.preventDefault();try{await onsubmit(Object.fromEntries(new FormData(f)),close)}catch(err){fail(err)}});
  (f.querySelector('input')||f.querySelector('button')).focus();
}
function voyageDialog(v){
  dialog(`<h3>${v?'Modifier le voyage':'Nouveau voyage'}</h3>
    <label for="d-nom">Nom</label><input id="d-nom" name="nom" required maxlength="80" value="${esc(v?.nom)}" placeholder="Japon 2027">
    <label for="d-pays">Pays</label><input id="d-pays" name="pays" maxlength="80" value="${esc(v?.pays)}">
    <div class="dgrid"><div><label for="d-debut">Début</label><input id="d-debut" type="date" name="debut" value="${esc(v?.debut)}"></div>
    <div><label for="d-fin">Fin</label><input id="d-fin" type="date" name="fin" value="${esc(v?.fin)}"></div></div>
    <label for="d-budget">Budget en euros (facultatif)</label><input id="d-budget" name="budget" inputmode="decimal" placeholder="2 000" value="${v&&v.budget!=null?esc(String(v.budget).replace('.',',')):''}">
    <div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn primary">${v?'Enregistrer':'Créer le voyage'}</button></div>`,
  async(f,close)=>{
    if(v){await api('PUT','voyage/'+v.id,f);close();await reloadVoyages();S.d=await api('GET','voyage/'+v.id);renderSide();renderMain();toast('Voyage enregistré.')}
    else{const r=await api('POST','voyage',f);close();await reloadVoyages();await select(r.id);toast('Voyage créé.')}
  });
}
function deleteVoyageDialog(){
  const v=S.d.voyage,nb=S.d.stats.nb;
  dialog(`<h3>Supprimer « ${esc(v.nom)} » ?</h3><p>${nb?`Les ${pl(nb,'note')} de ce voyage seront supprimées définitivement.`:'Ce voyage ne contient aucune note.'}</p>
    <div class="dactions"><button type="button" class="btn" data-close>Annuler</button><button class="btn danger">Supprimer le voyage</button></div>`,
  async(f,close)=>{
    await api('DELETE','voyage/'+v.id);close();await reloadVoyages();
    await select(S.voyages[0]?S.voyages[0].id:null);toast('Voyage supprimé.');
  });
}

/* ---------- événements ---------- */
document.addEventListener('click',async e=>{
  const t=e.target.closest('[data-act],[data-vid],[data-q],[data-mood]');
  if(!t)return;
  try{
    if(t.dataset.vid){if(+t.dataset.vid!==S.vid)await select(+t.dataset.vid);return}
    if(t.dataset.q!==undefined){setSearch(t.dataset.q);return}
    if(t.dataset.mood){const i=+t.dataset.mood;S.mood=S.mood===i?null:i;paintMood();return}
    const id=t.closest('.entry')?+t.closest('.entry').dataset.id:null;
    switch(t.dataset.act){
      case 'new-voyage':voyageDialog();break;
      case 'edit-voyage':voyageDialog(S.d.voyage);break;
      case 'del-voyage':deleteVoyageDialog();break;
      case 'cancel-edit':setComposer(null);break;
      case 'photo-add':S.fileJour=t.closest('.ticket').dataset.jour;$('#file').click();break;
      case 'photo-composer':S.fileJour=$('#f-jour').value||dateParDefaut();$('#file').click();break;
      case 'photo-open':lightbox(+t.dataset.pid);break;
      case 'photo-del':photoDelDialog(+t.dataset.pid);break;
      case 'photo-cover':{
        const pid=+t.dataset.pid,deja=S.d.voyage.couverture===pid;
        await api('PUT',`voyage/${S.vid}/couverture`,{photo_id:deja?null:pid});await refresh();
        toast(deja?'Couverture retirée.':'Couverture définie.');break}
      case 'fav':{
        const n=S.d.notes.find(x=>x.id===id);if(!n)break;
        await api('PUT',`note/${id}/favori`,{favori:!n.favori});await refresh();
        toast(n.favori?'Retiré des moments forts.':'Ajouté aux moments forts.');break}
      case 'composer-fav':S.fav=!S.fav;paintFav();break;
      case 'fav-filter':S.favOnly=!S.favOnly;paintTools();renderTimeline();break;
      case 'print':window.print();break;
      case 'goto-day':{
        const j=t.dataset.day,tk=document.querySelector(`.ticket[data-jour="${j}"]`);
        if(tk){tk.scrollIntoView({behavior:'smooth',block:'start'});tk.classList.remove('flash');void tk.offsetWidth;tk.classList.add('flash')}
        else{$('#f-jour').value=j;$('#composer').scrollIntoView({behavior:'smooth',block:'center'});$('#f-texte').focus();toast(`Date réglée au ${fCourt(j)}.`)}
        break}
      case 'edit':{const n=S.d.notes.find(x=>x.id===id);if(n){setComposer(n);$('#composer').scrollIntoView({behavior:'smooth',block:'center'});$('#f-texte').focus()}break}
      case 'del':{
        const n=S.d.notes.find(x=>x.id===id);if(!n)break;
        await api('DELETE','note/'+id);await refresh();
        toast('Note supprimée.','Annuler',async()=>{
          try{await api('POST','note',{voyage_id:S.vid,jour:n.jour,lieu:n.lieu,texte:n.texte,humeur:n.humeur,depense:n.depense,tags:n.tags,favori:n.favori});await refresh();toast('Note rétablie.')}catch(err){fail(err)}
        });break}
      case 'order':S.desc=!S.desc;paintTools();renderTimeline();api('PUT','reglage',{cle:'ordre',valeur:S.desc?'desc':'asc'}).catch(()=>{});break;
      case 'theme':S.theme=THEMES[(THEMES.indexOf(S.theme)+1)%3];applyTheme();api('PUT','reglage',{cle:'theme',valeur:S.theme}).catch(()=>{});break;
      case 'quit':
        await api('POST','quit');
        document.body.innerHTML='<div class="empty"><h2>Carnet est fermé</h2><p>Tu peux fermer cette fenêtre.</p></div>';
        window.close();break;
    }
  }catch(err){fail(err)}
});
let timer=null;
async function runSearch(){
  try{S.d=await api('GET',`voyage/${S.vid}?q=${enc(S.q)}`);renderTimeline()}catch(e){fail(e)}
}
function setSearch(q){S.q=q;const i=$('#q');if(i)i.value=q;runSearch();window.scrollTo(0,0);$('#timeline')?.scrollIntoView({behavior:'smooth'})}
document.addEventListener('input',e=>{
  if(e.target.id==='q'){clearTimeout(timer);timer=setTimeout(()=>{S.q=e.target.value.trim();runSearch()},180)}
  if(e.target.id==='f-texte')autosize();
});
document.addEventListener('submit',async e=>{
  if(e.target.id!=='composer')return;
  e.preventDefault();
  const body={texte:$('#f-texte').value,jour:$('#f-jour').value,lieu:$('#f-lieu').value,humeur:S.mood,depense:$('#f-dep').value,tags:$('#f-tags').value,favori:S.fav};
  try{
    if(S.edit){await api('PUT','note/'+S.edit.id,body);toast('Note enregistrée.')}
    else{body.voyage_id=S.vid;await api('POST','note',body);toast('Note ajoutée.')}
    setComposer(null);await refresh();$('#f-texte').focus();
  }catch(err){fail(err)}
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){const s=document.querySelector('.scrim');if(s){s.remove();return}if(S.edit)setComposer(null);return}
  if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&e.target.id==='f-texte'){e.preventDefault();$('#composer').requestSubmit();return}
  const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if((e.key==='/'&&!typing)||(e.key.toLowerCase()==='k'&&(e.ctrlKey||e.metaKey))){const i=$('#q');if(i){e.preventDefault();i.focus();i.select()}}
});

$('#file').addEventListener('change',async e=>{const fs=[...e.target.files];e.target.value='';await ajouterPhotos(fs,S.fileJour||dateParDefaut())});
const hasFiles=e=>e.dataTransfer&&[...e.dataTransfer.types].includes('Files');
const clearDrop=()=>document.querySelectorAll('.drop').forEach(x=>x.classList.remove('drop'));
document.addEventListener('dragover',e=>{
  if(!hasFiles(e))return;e.preventDefault();clearDrop();
  const z=e.target.closest&&e.target.closest('.ticket,.composer');if(z)z.classList.add('drop');
});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)clearDrop()});
document.addEventListener('drop',e=>{
  if(!hasFiles(e))return;e.preventDefault();clearDrop();
  const t=e.target.closest&&e.target.closest('.ticket'),c=e.target.closest&&e.target.closest('.composer');
  if(t)ajouterPhotos(e.dataTransfer.files,t.dataset.jour);
  else if(c)ajouterPhotos(e.dataTransfer.files,$('#f-jour').value||dateParDefaut());
});
document.addEventListener('paste',e=>{
  const fs=[...((e.clipboardData&&e.clipboardData.files)||[])].filter(f=>f.type.startsWith('image/'));
  if(!fs.length||!S.vid||!$('#composer'))return;
  e.preventDefault();ajouterPhotos(fs,$('#f-jour').value||dateParDefaut());
});

/* ---------- démarrage ---------- */
async function init(){
  $('#b-new').innerHTML=ic('plus')+'Nouveau voyage';
  $('#b-quit').innerHTML=ic('power')+'Quitter';
  $('#b-save').innerHTML=ic('disk')+'Sauvegarder mes données';
  try{
    const [r,v]=await Promise.all([api('GET','reglages'),api('GET','voyages')]);
    S.theme=THEMES.includes(r.theme)?r.theme:'auto';S.desc=r.ordre==='desc';applyTheme();
    S.voyages=v.voyages;
    const last=S.voyages.find(x=>x.id===+r.dernier)||S.voyages[0];
    await select(last?last.id:null);
  }catch(e){fail(e);renderMain()}
}
const ping=()=>fetch('/api/ping').catch(()=>{});
ping();setInterval(ping,2000);
addEventListener('pagehide',()=>{navigator.sendBeacon('/api/bye',new Blob(['{}'],{type:'application/json'}))});
init();
})();
</script>
</body>
</html>
'''


# ======================= SERVEUR (API + application) =======================
STATE = {"db": None, "lock": threading.Lock(), "ping": 0.0, "bye": None, "srv": None, "hotes": set()}


class Erreur(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Fichier:
    def __init__(self, nom, contenu, mime, cache=False):
        self.nom, self.contenu, self.mime, self.cache = nom, contenu, mime, cache


MAX_PHOTOS_JOUR = 3


def decoder_jpeg(b64, maxi):
    """Décode une image envoyée par la page ; seuls les vrais JPEG de taille raisonnable sont acceptés."""
    try:
        data = base64.b64decode(b64 or "", validate=True)
    except (ValueError, TypeError):
        raise ValueError("Image invalide.") from None
    if not data.startswith(b"\xff\xd8\xff") or len(data) > maxi:
        raise ValueError("Image invalide ou trop lourde.")
    return data


def texte(c, cle, maxi=200, requis=False):
    v = c.get(cle)
    v = "" if v is None else str(v).strip()
    if requis and not v:
        raise ValueError(f"Le champ « {cle} » est obligatoire.")
    if len(v) > maxi:
        raise ValueError(f"Le champ « {cle} » est trop long ({maxi} caractères maximum).")
    return v or None


def jour_iso(v):
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise ValueError("Date invalide (format AAAA-MM-JJ).") from None


def montant_optionnel(c, cle):
    v = c.get(cle)
    if v in (None, ""):
        return None
    try:
        x = float(re.sub(r"\s+", "", str(v)).replace(",", ".").replace("€", ""))
    except ValueError:
        raise ValueError("Montant invalide.") from None
    if not math.isfinite(x) or x < 0 or x > 1e9:
        raise ValueError("Montant invalide.")
    return x


def champs_voyage(c):
    nom = texte(c, "nom", 80, True)
    debut = jour_iso(c["debut"]) if c.get("debut") else None
    fin = jour_iso(c["fin"]) if c.get("fin") else None
    if debut and fin and fin < debut:
        raise ValueError("La date de fin précède la date de début.")
    return nom, texte(c, "pays", 80), debut, fin, montant_optionnel(c, "budget")


def champs_note(c):
    try:
        humeur = None if c.get("humeur") in (None, "") else int(c["humeur"])
    except (TypeError, ValueError):
        raise ValueError("Humeur invalide.") from None
    if humeur is not None and not 1 <= humeur <= 5:
        raise ValueError("L'humeur doit être comprise entre 1 et 5.")
    try:
        depense = float(re.sub(r"\s+", "", str(c.get("depense") or 0)).replace(",", ".").replace("€", "") or 0)
    except ValueError:
        raise ValueError("Montant invalide.") from None
    if not math.isfinite(depense) or abs(depense) > 1e9:
        raise ValueError("Montant invalide.")
    tags = []
    for t in re.split(r"[,\s]+", str(c.get("tags") or "")):
        t = t.strip().lstrip("#").lower()
        if t and t not in tags:
            tags.append(t)
    favori = 1 if c.get("favori") in (True, 1, "1", "true") else 0
    return (jour_iso(c.get("jour") or date.today().isoformat()), texte(c, "lieu", 120),
            texte(c, "texte", 20000, True), humeur, depense, ",".join(tags[:12]) or None, favori)


def lister_voyages(db):
    return [dict(r) for r in db.execute(
        "SELECT v.id,v.nom,v.pays,v.debut,v.fin,v.budget,v.couverture,COUNT(n.id) nb,COALESCE(SUM(n.depense),0) total "
        "FROM voyage v LEFT JOIN note n ON n.voyage_id=v.id GROUP BY v.id ORDER BY v.id DESC")]


def detail_voyage(db, vid, q):
    v = db.execute("SELECT * FROM voyage WHERE id=?", (vid,)).fetchone()
    if not v:
        raise Erreur(404, "Voyage introuvable.")
    if q:
        notes = rechercher(db, q, 500, vid)
    else:
        notes = db.execute("SELECT * FROM note WHERE voyage_id=? ORDER BY jour, id", (vid,)).fetchall()
    g = db.execute(
        "SELECT COUNT(*) nb, COALESCE(SUM(depense),0) total, AVG(humeur) humeur, COUNT(DISTINCT lieu) lieux, "
        "COALESCE(SUM(favori),0) favoris "
        "FROM note WHERE voyage_id=?", (vid,)).fetchone()
    stats = dict(g)
    stats["par_jour"] = [dict(r) for r in db.execute(
        "SELECT jour, COUNT(*) nb, COALESCE(SUM(depense),0) total, COALESCE(SUM(favori),0) favoris FROM note WHERE voyage_id=? "
        "GROUP BY jour ORDER BY jour", (vid,))]
    stats["top_lieux"] = [dict(r) for r in db.execute(
        "SELECT lieu, COUNT(*) nb FROM note WHERE voyage_id=? AND lieu IS NOT NULL "
        "GROUP BY lieu ORDER BY nb DESC, lieu LIMIT 5", (vid,))]
    lieux = [r["lieu"] for r in db.execute(
        "SELECT DISTINCT lieu FROM note WHERE lieu IS NOT NULL ORDER BY lieu LIMIT 300")]
    photos = [dict(r) for r in db.execute("SELECT id, jour FROM photo WHERE voyage_id=? ORDER BY jour, id", (vid,))]
    return {"voyage": dict(v), "notes": [dict(n) for n in notes], "stats": stats, "lieux": lieux, "photos": photos}


def api(m, chemin, q, c):
    db = STATE["db"]
    p = chemin.strip("/").split("/")
    num = lambda i: len(p) > i and p[i].isdigit()  # noqa: E731

    if p == ["ping"] and m == "GET":
        STATE["ping"], STATE["bye"] = time.time(), None
        return {"ok": True, "app": "carnet"}
    if p == ["bye"] and m == "POST":
        STATE["bye"] = time.time()
        return {"ok": True}
    if p == ["quit"] and m == "POST":
        threading.Timer(0.3, arreter).start()
        return {"ok": True}
    if p == ["reglages"] and m == "GET":
        return {r["cle"]: r["valeur"] for r in db.execute("SELECT cle, valeur FROM reglage")}
    if p == ["reglage"] and m == "PUT":
        cle, val = texte(c, "cle", 30, True), texte(c, "valeur", 100) or ""
        if cle not in ("theme", "dernier", "ordre"):
            raise ValueError("Réglage inconnu.")
        with db:
            db.execute("INSERT INTO reglage(cle,valeur) VALUES(?,?) "
                       "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur", (cle, val))
        return {"ok": True}

    if p == ["sauvegarde"] and m == "GET":
        return Fichier(f"carnet-sauvegarde-{date.today().isoformat()}.db", sauvegarde_octets(db), "application/octet-stream")
    if p == ["voyages"] and m == "GET":
        return {"voyages": lister_voyages(db)}
    if p == ["voyage"] and m == "POST":
        nom, pays, debut, fin, budget = champs_voyage(c)
        with db:
            db.execute(
                "INSERT INTO voyage(nom,pays,debut,fin,budget) VALUES(?,?,?,?,?) ON CONFLICT(nom) DO UPDATE SET "
                "pays=COALESCE(excluded.pays,pays),debut=COALESCE(excluded.debut,debut),"
                "fin=COALESCE(excluded.fin,fin),budget=COALESCE(excluded.budget,budget)",
                (nom, pays, debut, fin, budget))
        return {"id": db.execute("SELECT id FROM voyage WHERE nom=?", (nom,)).fetchone()["id"]}
    if len(p) == 2 and p[0] == "voyage" and num(1):
        vid = int(p[1])
        if m == "GET":
            return detail_voyage(db, vid, (q.get("q") or "").strip())
        if m == "PUT":
            nom, pays, debut, fin, budget = champs_voyage(c)
            try:
                with db:
                    cur = db.execute("UPDATE voyage SET nom=?,pays=?,debut=?,fin=?,budget=? WHERE id=?",
                                     (nom, pays, debut, fin, budget, vid))
            except sqlite3.IntegrityError:
                raise Erreur(409, "Un voyage porte déjà ce nom.") from None
            if not cur.rowcount:
                raise Erreur(404, "Voyage introuvable.")
            return {"ok": True}
        if m == "DELETE":
            with db:
                db.execute("DELETE FROM voyage WHERE id=?", (vid,))
            return {"ok": True}
    if len(p) == 3 and p[0] == "voyage" and num(1) and p[2] == "couverture" and m == "PUT":
        vid = int(p[1])
        pid = c.get("photo_id")
        if pid in (None, ""):
            pid = None
        else:
            pid = int(pid)
            if not db.execute("SELECT 1 FROM photo WHERE id=? AND voyage_id=?", (pid, vid)).fetchone():
                raise Erreur(404, "Photo introuvable.")
        with db:
            cur = db.execute("UPDATE voyage SET couverture=? WHERE id=?", (pid, vid))
        if not cur.rowcount:
            raise Erreur(404, "Voyage introuvable.")
        return {"ok": True, "couverture": pid}
    if len(p) == 3 and p[0] == "voyage" and num(1) and p[2] == "export" and m == "GET":
        vid = int(p[1])
        v = db.execute("SELECT nom FROM voyage WHERE id=?", (vid,)).fetchone()
        if not v:
            raise Erreur(404, "Voyage introuvable.")
        fmt = q.get("format") if q.get("format") in ("json", "zip") else "md"
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", v["nom"]).strip("_") or "carnet"
        if fmt == "zip":  # carnet.md + dossier photos/
            images, compteur, buf = defaultdict(list), defaultdict(int), io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # les JPEG sont déjà compressés
                for f in db.execute("SELECT jour, grande FROM photo WHERE voyage_id=? ORDER BY jour, id", (vid,)):
                    compteur[f["jour"]] += 1
                    chemin = f"photos/{f['jour']}_{compteur[f['jour']]}.jpg"
                    images[f["jour"]].append(chemin)
                    z.writestr(chemin, f["grande"])
                z.writestr(f"{base}.md", exporter(db, v["nom"], vid, "md", images).encode("utf-8"))
            return Fichier(f"{base}.zip", buf.getvalue(), "application/zip")
        mime = "application/json" if fmt == "json" else "text/markdown"
        return Fichier(f"{base}.{fmt}", exporter(db, v["nom"], vid, fmt).encode("utf-8"), mime + "; charset=utf-8")

    if p == ["note"] and m == "POST":
        vid = int(c.get("voyage_id") or 0)
        if not db.execute("SELECT 1 FROM voyage WHERE id=?", (vid,)).fetchone():
            raise Erreur(404, "Voyage introuvable.")
        with db:
            cur = db.execute(
                "INSERT INTO note(voyage_id,jour,lieu,texte,humeur,depense,tags,favori) VALUES(?,?,?,?,?,?,?,?)",
                (vid, *champs_note(c)))
        return dict(db.execute("SELECT * FROM note WHERE id=?", (cur.lastrowid,)).fetchone())
    if len(p) == 3 and p[0] == "note" and num(1) and p[2] == "favori" and m == "PUT":
        nid = int(p[1])
        with db:
            cur = db.execute("UPDATE note SET favori=? WHERE id=?",
                             (1 if c.get("favori") in (True, 1, "1", "true") else 0, nid))
        if not cur.rowcount:
            raise Erreur(404, "Note introuvable.")
        return dict(db.execute("SELECT * FROM note WHERE id=?", (nid,)).fetchone())
    if len(p) == 2 and p[0] == "note" and num(1):
        nid = int(p[1])
        if m == "PUT":
            with db:
                cur = db.execute(
                    "UPDATE note SET jour=?,lieu=?,texte=?,humeur=?,depense=?,tags=?,favori=? WHERE id=?",
                    (*champs_note(c), nid))
            if not cur.rowcount:
                raise Erreur(404, "Note introuvable.")
            return dict(db.execute("SELECT * FROM note WHERE id=?", (nid,)).fetchone())
        if m == "DELETE":
            with db:
                cur = db.execute("DELETE FROM note WHERE id=?", (nid,))
            if not cur.rowcount:
                raise Erreur(404, "Note introuvable.")
            return {"ok": True}
    if p == ["photo"] and m == "POST":
        vid = int(c.get("voyage_id") or 0)
        if not db.execute("SELECT 1 FROM voyage WHERE id=?", (vid,)).fetchone():
            raise Erreur(404, "Voyage introuvable.")
        jour = jour_iso(c.get("jour"))
        deja = db.execute("SELECT COUNT(*) FROM photo WHERE voyage_id=? AND jour=?", (vid, jour)).fetchone()[0]
        if deja >= MAX_PHOTOS_JOUR:
            raise Erreur(409, f"{MAX_PHOTOS_JOUR} photos maximum par jour.")
        grande, mini = decoder_jpeg(c.get("grande"), 8_000_000), decoder_jpeg(c.get("mini"), 800_000)
        with db:
            cur = db.execute("INSERT INTO photo(voyage_id,jour,grande,mini) VALUES(?,?,?,?)", (vid, jour, grande, mini))
        return {"id": cur.lastrowid, "jour": jour}
    if len(p) == 2 and p[0] == "photo" and num(1):
        pid = int(p[1])
        if m == "GET":
            row = db.execute("SELECT grande, mini FROM photo WHERE id=?", (pid,)).fetchone()
            if not row:
                raise Erreur(404, "Photo introuvable.")
            return Fichier(None, row["mini"] if q.get("v") == "mini" else row["grande"], "image/jpeg", cache=True)
        if m == "DELETE":
            with db:
                db.execute("UPDATE voyage SET couverture=NULL WHERE couverture=?", (pid,))
                cur = db.execute("DELETE FROM photo WHERE id=?", (pid,))
            if not cur.rowcount:
                raise Erreur(404, "Photo introuvable.")
            return {"ok": True}
    raise Erreur(404, "Introuvable.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _envoyer(self, code, corps=b"", ctype="application/json; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corps)))
        extra = dict(headers)
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def _json(self, code, obj):
        self._envoyer(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _verif(self, methode):
        hote = self.headers.get("Host")
        if hote not in STATE["hotes"]:  # protège du DNS rebinding
            self._json(403, {"erreur": "Hôte refusé."})
            return False
        origine = self.headers.get("Origin")
        if origine and urlparse(origine).netloc != hote:  # protège des requêtes d'autres sites
            self._json(403, {"erreur": "Origine refusée."})
            return False
        if methode in ("POST", "PUT") and "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json(415, {"erreur": "Format non pris en charge."})
            return False
        return True

    def _traiter(self, methode):
        if not self._verif(methode):
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if methode == "GET" and u.path == "/":
                return self._envoyer(200, APP_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                raise Erreur(404, "Page introuvable.")
            corps = {}
            if methode in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > (14_000_000 if u.path == "/api/photo" else 2_000_000):
                    raise Erreur(413, "Données trop volumineuses.")
                corps = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(corps, dict):
                    raise ValueError("Données invalides.")
            with STATE["lock"]:
                res = api(methode, u.path[5:], q, corps)
        except Erreur as e:
            return self._json(e.code, {"erreur": str(e)})
        except (ValueError, KeyError) as e:
            return self._json(400, {"erreur": str(e) or "Données invalides."})
        except sqlite3.IntegrityError:
            return self._json(409, {"erreur": "Cette donnée existe déjà."})
        except Exception as e:  # noqa: BLE001
            print("Erreur interne :", repr(e), file=sys.stderr)
            return self._json(500, {"erreur": "Erreur interne."})
        if isinstance(res, Fichier):
            h = []
            if res.nom:
                h.append(("Content-Disposition", f'attachment; filename="{res.nom}"'))
            if res.cache:  # une photo ne change jamais : le navigateur peut la garder
                h.append(("Cache-Control", "private, max-age=31536000, immutable"))
            return self._envoyer(200, res.contenu, res.mime, h)
        self._json(200, res)

    def do_GET(self):
        self._traiter("GET")

    def do_POST(self):
        self._traiter("POST")

    def do_PUT(self):
        self._traiter("PUT")

    def do_DELETE(self):
        self._traiter("DELETE")


def arreter():
    srv = STATE["srv"]
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()


def carnet_actif(port):
    """Vrai si un Carnet tourne déjà sur ce port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "carnet"
    except Exception:  # noqa: BLE001
        return False


def ouvrir_serveur(port, souple):
    """Retourne (serveur, port) ; (None, port) si un Carnet tourne déjà sur ce port."""
    essais = [port, 0] if souple else [port]
    for p in essais:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError:
            if not souple:
                raise
            if p == port and carnet_actif(port):
                return None, port
            continue
        port = srv.server_address[1]
        STATE["srv"] = srv
        STATE["hotes"] = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return srv, port
    raise OSError("Aucun port disponible.")


def trouver_navigateur():
    """Cherche Edge / Chrome / Chromium / Brave : ils savent ouvrir une page comme une application."""
    cands = []
    if sys.platform.startswith("win"):
        for cle in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
            base = os.environ.get(cle)
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                          Path(base) / "Google/Chrome/Application/chrome.exe",
                          Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"]
    elif sys.platform == "darwin":
        cands = [Path(f"/Applications/{n}.app/Contents/MacOS/{n}")
                 for n in ("Microsoft Edge", "Google Chrome", "Chromium", "Brave Browser")]
    for nom in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
        w = shutil.which(nom)
        if w:
            cands.append(Path(w))
    return next((str(c) for c in cands if c.exists()), None)


def lancer_fenetre(url):
    exe = trouver_navigateur()
    if exe:
        profil = Path.home() / ".carnet_profil"
        try:
            subprocess.Popen(
                [exe, f"--app={url}", "--window-size=1280,860", f"--user-data-dir={profil}",
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)  # repli : navigateur par défaut


def surveiller():
    """Arrête le serveur quand la fenêtre est fermée (signal 'bye') ou n'a jamais été ouverte."""
    debut = time.time()
    while True:
        time.sleep(1)
        now, bye, ping = time.time(), STATE["bye"], STATE["ping"]
        if bye and now - bye > 8:
            break
        if not ping and now - debut > 90:
            break
        if ping and now - ping > 180:
            break
    arreter()


def cmd_gui(db, a):
    STATE["db"] = db
    srv, port = ouvrir_serveur(8765, souple=True)
    url = f"http://127.0.0.1:{port}"
    lancer_fenetre(url)
    if srv is None:  # une fenêtre Carnet existe déjà : on s'y rattache
        return
    threading.Thread(target=surveiller, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_web(db, a):
    STATE["db"] = db
    srv, port = ouvrir_serveur(a.port, souple=False)
    url = f"http://127.0.0.1:{port}"
    if not a.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    print(f"Carnet de voyage -> {url}  (Ctrl+C pour arreter)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ======================= POINT D'ENTRÉE =======================
def main():
    # Application sans console (.exe) : stdout/stderr n'existent pas, on les neutralise
    for nom in ("stdout", "stderr"):
        if getattr(sys, nom) is None:
            setattr(sys, nom, open(os.devnull, "w"))
    args = build_parser().parse_args(sys.argv[1:] or ["gui"])
    db = connect()
    try:
        args.fn(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()

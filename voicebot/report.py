"""Rapport PDF de session (statistiques, graphiques, palmarès)."""
from pathlib import Path

from .config import SET
from .utils import fmt_dur


def pdf_safe(x) -> str:
    """Les polices PDF standard ne gèrent que le jeu cp1252 : on retire emojis et caractères exotiques."""
    return str(x).encode("cp1252", "ignore").decode("cp1252").strip()


def build_report(stats: dict, rows: list, rec: "SessionRecorder", path: Path) -> Path:
    from xml.sax.saxutils import escape
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def t(x) -> str:                       # texte pour un Paragraph (balisage XML échappé)
        return escape(pdf_safe(x))

    def nm(u: dict, n: int = 28) -> str:   # nom pour une cellule / un dessin
        return pdf_safe(u["name"])[:n] or "(?)"

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    palette = [colors.HexColor(c) for c in ("#4C6EF5", "#F76707", "#12B886", "#E64980", "#7950F2",
                                            "#FAB005", "#15AABF", "#82C91E", "#FA5252", "#868E96")]
    users = stats["users"]
    color_of = {u["id"]: palette[i % len(palette)] for i, u in enumerate(users)}
    width = A4[0] - 4 * cm
    grey = colors.HexColor("#F1F3F5")

    def bar_chart(items, fmt=str):
        """items : [(libellé, valeur, couleur)] -> barres horizontales."""
        row_h, label_w, val_w = 18, 120, 50
        h = row_h * len(items) + 6
        d = Drawing(width, h)
        vmax = max((v for _, v, _ in items), default=0) or 1
        plot_w = width - label_w - val_w
        for i, (label, v, col) in enumerate(items):
            y = h - (i + 1) * row_h
            w = max(1.0, plot_w * v / vmax)
            d.add(String(0, y + 5, label, fontName="Helvetica", fontSize=9))
            d.add(Rect(label_w, y + 2, w, row_h - 6, fillColor=col, strokeColor=None))
            d.add(String(label_w + w + 4, y + 5, fmt(v), fontName="Helvetica", fontSize=9))
        return d

    def timeline(shown):
        """Une ligne par intervenant, un rectangle par prise de parole sur l'axe du temps."""
        lane_h, label_w, axis_h = 16, 120, 18
        n = len(shown)
        total = max(stats["duration_s"], max((float(r["end"]) for r in rows), default=0.0), 1.0)
        plot_w = width - label_w - 8
        d = Drawing(width, lane_h * n + axis_h + 4)
        lane_y = {u["id"]: axis_h + (n - 1 - i) * lane_h for i, u in enumerate(shown)}
        for u in shown:
            y = lane_y[u["id"]]
            d.add(String(0, y + 4, nm(u, 24), fontName="Helvetica", fontSize=9))
            d.add(Rect(label_w, y + 1, plot_w, lane_h - 3, fillColor=grey, strokeColor=None))
        for r in rows:
            uid = str(r["user"])
            if uid not in lane_y:
                continue
            s, e = float(r["start"]), float(r["end"])
            d.add(Rect(label_w + plot_w * s / total, lane_y[uid] + 1, max(1.0, plot_w * (e - s) / total),
                       lane_h - 3, fillColor=color_of[uid], strokeColor=None))
        step = next((s for s in (30, 60, 120, 300, 600, 900, 1800, 3600, 7200) if total / s <= 10), 14400)
        tick = 0
        while tick <= total:
            x = label_w + plot_w * tick / total
            d.add(Line(x, axis_h - 4, x, axis_h, strokeColor=colors.grey))
            d.add(String(x - 12, 2, fmt_dur(tick), fontName="Helvetica", fontSize=7, fillColor=colors.grey))
            tick += step
        return d

    story = [Paragraph("Rapport de session", styles["Title"]),
             Paragraph(f"Session {t(rec.session_id)} — démarrée le "
                       f"{rec.t0.astimezone().strftime('%d/%m/%Y à %H:%M')}", styles["Normal"]),
             Spacer(1, 12)]

    sil = stats["longest_silence"]
    summary = [["Durée", fmt_dur(stats["duration_s"])],
               ["Participants", str(stats["participants"])],
               ["Lignes / mots", f"{stats['lines']} / {stats['words']}"]]
    if stats["lines"]:
        summary.append(["Temps de parole",
                        f"{stats['speech_pct']:.0f} % du temps (silence : {100 - stats['speech_pct']:.0f} %)"])
    if sil["duration_s"] >= SET["stats.silence_affiche_min_s"]:
        summary.append(["Plus long silence", f"{fmt_dur(sil['duration_s'])} (à {fmt_dur(sil['at_s'])})"])
    if stats["busiest_minute"]:
        b = stats["busiest_minute"]
        summary.append(["Minute la plus animée", f"{fmt_dur(b['at_s'])} ({b['words']} mots)"])
    st = Table(summary, colWidths=[5 * cm, width - 5 * cm])
    st.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    story += [st, Spacer(1, 10)]

    if not stats["lines"]:
        story.append(Paragraph("Aucune parole transcrite pendant cette session.", body))
    else:
        story.append(Paragraph("Par intervenant", styles["Heading2"]))
        data = [["Nom", "Lignes", "Mots", "Parole", "Mots/min", "Part", "Mots/ligne"]]
        for u in users:
            data.append([nm(u), u["lines"], u["words"], fmt_dur(u["speech_s"]),
                         f"{u['words_per_min']:.0f}" if u["words_per_min"] else "-",
                         f"{u['share_pct']:.0f} %", f"{u['words_per_line']:.1f}"])
        col = 2.1 * cm
        ut = Table(data, colWidths=[width - 6 * col] + [col] * 6, repeatRows=1)
        ut.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4C6EF5")),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, grey]),
                                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
        story += [ut, Spacer(1, 6)]

        shown = [u for u in users if u["words"] > 0][:SET["rapport.max_intervenants_graphiques"]]
        story += [Paragraph("Mots par intervenant", styles["Heading2"]),
                  bar_chart([(nm(u, 22), u["words"], color_of[u["id"]]) for u in shown]),
                  Paragraph("Temps de parole par intervenant", styles["Heading2"]),
                  bar_chart([(nm(u, 22), u["speech_s"], color_of[u["id"]]) for u in shown], fmt_dur),
                  Paragraph("Qui parle quand", styles["Heading2"]), timeline(shown)]

    if stats["awards"] or stats["ghosts"]:
        story.append(Paragraph("Palmarès", styles["Heading2"]))
        for a in stats["awards"]:
            story.append(Paragraph(f"<b>{t(a['title'])}</b> : {t(a['name'])} — {t(a['detail'])}", body))
        if stats["ghosts"]:
            story.append(Paragraph("<b>Les silencieux</b> : " + ", ".join(t(g) for g in stats["ghosts"])
                                   + " (présent(s) mais sans un mot)", body))
    favs = [f"{t(u['name'])} : « {t(u['favorite_word']['word'])} » ×{u['favorite_word']['count']}"
            for u in users if u["favorite_word"]]
    if stats["top_words"] or favs:
        story.append(Paragraph("Vocabulaire", styles["Heading2"]))
        if stats["top_words"]:
            story.append(Paragraph("<b>Mots les plus utilisés</b> : " + ", ".join(
                f"« {t(w)} » ×{c}" for w, c in stats["top_words"]), body))
        if favs:
            story.append(Paragraph("<b>Mot favori</b> — " + " ; ".join(favs), body))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(2 * cm, 1.2 * cm, f"Session {pdf_safe(rec.session_id)}")
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm,
                      bottomMargin=2 * cm, title=f"Rapport de session {pdf_safe(rec.session_id)}",
                      author="Bot Discord").build(story, onFirstPage=footer, onLaterPages=footer)
    return path

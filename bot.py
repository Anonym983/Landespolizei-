"""Polizei-Bot (discord.py 2.x) - Personal-Embeds, Aktivitaetscheck, Anwalt,
Platzverweis, Ausweissystem. Start: python bot.py  (Token in DISCORD_TOKEN)"""
import os, io, json, copy, random, string, time, datetime as dt
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: Befehle sofort auf deinem Server
DB_FILE = os.getenv("DB_FILE", "data.json")

BLAU, GRUEN, ORANGE, ROT = 0x2B5BFF, 0x5BFF3A, 0xF5A623, 0xE5301F

DEFAULT_DB = {"config": {}, "lpn": {}, "sperren": {}, "disziplin": [],
              "bewerbungssperren": {}, "aktivitaet": {}, "dab_warns": {},
              "ausweise": {}, "ausweis_anfragen": {}, "anwalt_cd": {},
              "fahndungen": {}, "logkanaele": {}, "auto_log": {},
              "dienst": {"aktiv": {}, "gesamt": {}, "panel": None}}


def load_db():
    data = {}
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding="utf-8") as f:
            data = json.load(f)
    for k, v in DEFAULT_DB.items():
        data.setdefault(k, copy.deepcopy(v))
    return data


db = load_db()


def save():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)


# ---------- Helfer ----------
def cfg(key):
    return db["config"].get(key)


def now():
    return dt.datetime.now(dt.timezone.utc)


def future(days):
    return now() + dt.timedelta(days=days)


def ts(d):
    return discord.utils.format_dt(d, "F")


def active(iso):
    return bool(iso) and dt.datetime.fromisoformat(iso) > now()


def bewerbung_gesperrt(user_id):
    """Fuer deine Bewerbungsseite/-Bot: ist der Nutzer gesperrt?"""
    return active(db["bewerbungssperren"].get(str(user_id)))


def hauptfarbe():
    return cfg("farbe") or BLAU


def embed(title, desc, color=BLAU, panel=False):
    if color == BLAU:
        color = hauptfarbe()
    e = discord.Embed(title=title, description=desc, color=color, timestamp=now())
    e.set_footer(text=cfg("footer") or "Landespolizei Nova")
    if cfg("logo"):
        e.set_thumbnail(url=cfg("logo"))
    if panel and cfg("banner"):
        e.set_image(url=cfg("banner"))
    return e


def person(m):
    lpn = db["lpn"].get(str(m.id))
    return f"{m.mention} [LPN{lpn}]" if lpn else m.mention


def has_any(member, *keys):
    if member.guild_permissions.administrator:
        return True
    ids = {cfg(k) for k in keys if cfg(k)}
    return any(r.id in ids for r in member.roles)


def only(*keys):
    async def pred(i: discord.Interaction):
        if has_any(i.user, *keys):
            return True
        raise app_commands.CheckFailure("❌ Dafür fehlt dir die Berechtigung.")
    return app_commands.check(pred)


async def change_roles(member, add=(), remove=()):
    try:
        add = [r for r in add if r]
        remove = [r for r in remove if r]
        if add:
            await member.add_roles(*add, reason="Polizeibot")
        if remove:
            await member.remove_roles(*remove, reason="Polizeibot")
    except discord.Forbidden:
        pass


async def mirror(guild, ereignis, emb, skip_id=None):
    """Kopiert ein Ereignis zusätzlich in die zugeordnete Team-Log-Kategorie."""
    k = db["logkanaele"].get(db["auto_log"].get(ereignis, ""))
    ch = guild.get_channel(k["channel"]) if k else None
    if ch and ch.id != skip_id:
        await ch.send(embed=emb)


async def send_log(i, key, emb):
    if not i.response.is_done():
        await i.response.defer(ephemeral=True)
    ch = i.guild.get_channel(cfg(key) or 0) or i.channel
    await ch.send(embed=emb)
    await mirror(i.guild, key[3:], emb, ch.id)
    if key == "ch_personal":
        await refresh_teamliste(i.guild)
    await i.followup.send("✅ Eintrag erstellt.", ephemeral=True)


# ---------- Bot ----------
intents = discord.Intents.default()
intents.members = True          # im Developer Portal aktivieren!
intents.message_content = True  # im Developer Portal aktivieren!


class PolizeiBot(commands.Bot):
    async def setup_hook(self):
        for v in (PlatzverweisView(), AktivitaetView(), AusweisPanelView(), AusweisReviewView(), FahndungView(), DienstView()):
            self.add_view(v)
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        checker.start()


bot = PolizeiBot(command_prefix="!", intents=intents)


@bot.tree.error
async def on_tree_error(i: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = str(error)
    else:
        msg = "❌ Es ist ein Fehler aufgetreten."
        print("Fehler:", repr(error))
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


# ---------- Einrichtung ----------
KANAELE = {"personal": "Personal-Log (Einstellung, Uprank, Entlassung)",
           "disziplin": "Disziplinar-Log", "bewerbung": "Bewerbungssperren-Log",
           "ausweis_pruefung": "Ausweis-Prüfung (Anträge)",
           "fahndung": "Fahndungen",
           "dienst": "Dienst-Log (Dienstzeiten)"}
ROLLEN = {"direktion": "Direktion (darf alle Personal-Befehle)",
          "polizei": "Polizei (Aktivitätscheck)",
          "pflichtverteidiger": "Pflichtverteidiger (Anwalt-Ping)",
          "ausweis_team": "Ausweis-Team (Anträge prüfen)",
          "imdienst": "Im-Dienst-Rolle (wird automatisch vergeben)"}

setup = app_commands.Group(name="setup", description="Bot einrichten",
                           default_permissions=discord.Permissions(administrator=True))


@setup.command(name="kanal", description="Kanal für eine Funktion festlegen")
@app_commands.choices(typ=[app_commands.Choice(name=v, value=k) for k, v in KANAELE.items()])
async def setup_kanal(i: discord.Interaction, typ: app_commands.Choice[str], kanal: discord.TextChannel):
    db["config"]["ch_" + typ.value] = kanal.id
    save()
    await i.response.send_message(f"✅ {typ.name}: {kanal.mention}", ephemeral=True)


@setup.command(name="rolle", description="Rolle für eine Funktion festlegen")
@app_commands.choices(typ=[app_commands.Choice(name=v, value=k) for k, v in ROLLEN.items()])
async def setup_rolle(i: discord.Interaction, typ: app_commands.Choice[str], rolle: discord.Role):
    db["config"]["rl_" + typ.value] = rolle.id
    save()
    await i.response.send_message(f"✅ {typ.name}: {rolle.mention}", ephemeral=True)


@setup.command(name="footer", description="Footer-Text der Embeds ändern")
async def setup_footer(i: discord.Interaction, text: str):
    db["config"]["footer"] = text
    save()
    await i.response.send_message("✅ Footer gespeichert.", ephemeral=True)


AUTO_EREIGNISSE = {"personal": "Personal-Aktionen (Einstellung, Uprank, Entlassung …)",
                   "disziplin": "Disziplinarmaßnahmen", "bewerbung": "Bewerbungssperren",
                   "fahndung": "Fahndungen", "dienst": "Dienstzeiten"}


async def kat_autocomplete(i: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in db["logkanaele"]
            if current.lower() in n.lower()][:25]


@setup.command(name="logkanal", description="Team-Log-Kategorie anlegen (z.B. Leitungsebene, Ausbilder)")
async def setup_logkanal(i: discord.Interaction, name: str, kanal: discord.TextChannel,
                         rolle: discord.Role | None = None):
    db["logkanaele"][name.strip()] = {"channel": kanal.id, "rolle": rolle.id if rolle else None}
    save()
    extra = f" – schreiben darf: {rolle.mention}" if rolle else ""
    await i.response.send_message(f"✅ Log-Kategorie **{name}** → {kanal.mention}{extra}", ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


@setup.command(name="logkanal_entfernen", description="Team-Log-Kategorie löschen")
@app_commands.autocomplete(name=kat_autocomplete)
async def setup_logkanal_entf(i: discord.Interaction, name: str):
    db["logkanaele"].pop(name, None)
    for k in [k for k, v in db["auto_log"].items() if v == name]:
        db["auto_log"].pop(k)
    save()
    await i.response.send_message("✅ Entfernt.", ephemeral=True)


@setup.command(name="auto_log", description="Ereignis automatisch zusätzlich in eine Log-Kategorie kopieren")
@app_commands.choices(ereignis=[app_commands.Choice(name=v, value=k) for k, v in AUTO_EREIGNISSE.items()])
@app_commands.autocomplete(kategorie=kat_autocomplete)
async def setup_autolog(i: discord.Interaction, ereignis: app_commands.Choice[str], kategorie: str):
    if kategorie not in db["logkanaele"]:
        return await i.response.send_message("❌ Diese Log-Kategorie gibt es nicht.", ephemeral=True)
    db["auto_log"][ereignis.value] = kategorie
    save()
    await i.response.send_message(f"✅ {ereignis.name} werden zusätzlich in **{kategorie}** kopiert.",
                                  ephemeral=True)


@setup.command(name="status", description="Zeigt, was schon eingerichtet ist und was noch fehlt")
async def setup_status(i: discord.Interaction):
    g = i.guild

    def kanal(k):
        ch = g.get_channel(cfg("ch_" + k) or 0)
        return ch.mention if ch else "❌ fehlt"

    def rolle(k):
        r = g.get_role(cfg("rl_" + k) or 0)
        return r.mention if r else "❌ fehlt"

    e = embed("⚙️ Einrichtungs-Status", "Hier siehst du, was schon eingerichtet ist.", BLAU)
    e.add_field(name="Kanäle", value="\n".join(f"{v}: {kanal(k)}" for k, v in KANAELE.items()), inline=False)
    e.add_field(name="Rollen", value="\n".join(f"{v}: {rolle(k)}" for k, v in ROLLEN.items()), inline=False)
    logk = "\n".join(f"**{n}** → <#{v['channel']}>" for n, v in db["logkanaele"].items()) or "–"
    e.add_field(name="Team-Log-Kategorien", value=logk[:1000], inline=False)
    raenge = [g.get_role(r) for r in db["config"].get("raenge", [])]
    e.add_field(name="Ränge (Teamliste)", value=" › ".join(r.mention for r in raenge if r) or "❌ keine", inline=False)
    farbe = "#%06X" % hauptfarbe()
    e.add_field(name="Design", value=f"Logo: {'✅' if cfg('logo') else '–'} · Banner: {'✅' if cfg('banner') else '–'} · Farbe: {farbe}", inline=False)
    pruefen = raenge + [g.get_role(cfg("rl_polizei") or 0), g.get_role(cfg("rl_imdienst") or 0)]
    zu_hoch = [r for r in pruefen if r and r >= g.me.top_role]
    if zu_hoch:
        e.add_field(name="⚠️ Rollen-Reihenfolge",
                    value="Die Bot-Rolle muss in den Servereinstellungen **über** diesen Rollen stehen:\n"
                          + " ".join(r.mention for r in zu_hoch), inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


@setup.command(name="logo", description="Logo (Bild-Link) für alle Embeds setzen")
async def setup_logo(i: discord.Interaction, url: str):
    if not url.startswith("http"):
        return await i.response.send_message("❌ Bitte einen Bild-Link (https://…) angeben.", ephemeral=True)
    db["config"]["logo"] = url
    save()
    await i.response.send_message(embed=embed("✅ Logo gesetzt", "So sehen deine Embeds jetzt aus."), ephemeral=True)


@setup.command(name="banner", description="Banner (Bild-Link) für alle Panels setzen")
async def setup_banner(i: discord.Interaction, url: str):
    if not url.startswith("http"):
        return await i.response.send_message("❌ Bitte einen Bild-Link (https://…) angeben.", ephemeral=True)
    db["config"]["banner"] = url
    save()
    await i.response.send_message(embed=embed("✅ Banner gesetzt", "So sehen deine Panels jetzt aus.", panel=True), ephemeral=True)


@setup.command(name="farbe", description="Hauptfarbe als HEX-Code setzen, z.B. 2B5BFF")
async def setup_farbe(i: discord.Interaction, hexwert: str):
    try:
        wert = int(hexwert.strip().lstrip("#"), 16)
        assert 0 <= wert <= 0xFFFFFF
    except (ValueError, AssertionError):
        return await i.response.send_message("❌ Bitte einen HEX-Code wie 2B5BFF angeben.", ephemeral=True)
    db["config"]["farbe"] = wert
    save()
    await i.response.send_message(embed=embed("✅ Farbe gesetzt", "So sieht die neue Hauptfarbe aus."), ephemeral=True)


@setup.command(name="rang_hinzufuegen", description="Rang für die Teamliste hinzufügen (höchster Rang zuerst)")
async def setup_rang_add(i: discord.Interaction, rolle: discord.Role):
    raenge = db["config"].setdefault("raenge", [])
    if rolle.id not in raenge:
        raenge.append(rolle.id)
    save()
    await i.response.send_message(f"✅ {rolle.mention} ist jetzt Rang Nr. {raenge.index(rolle.id) + 1}.",
                                  ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@setup.command(name="rang_entfernen", description="Rang aus der Teamliste entfernen (ohne Angabe: alle)")
async def setup_rang_del(i: discord.Interaction, rolle: discord.Role | None = None):
    if rolle:
        db["config"]["raenge"] = [r for r in db["config"].get("raenge", []) if r != rolle.id]
    else:
        db["config"]["raenge"] = []
    save()
    await i.response.send_message("✅ Erledigt.", ephemeral=True)


bot.tree.add_command(setup)


# ---------- Personal-Befehle ----------
@bot.tree.command(description="Neueinstellung eintragen")
@only("rl_direktion")
async def einstellung(i: discord.Interaction, user: discord.Member, lpn: int,
                      dienstgrad: discord.Role, notiz: str):
    await i.response.defer(ephemeral=True)
    db["lpn"][str(user.id)] = lpn
    save()
    await change_roles(user, add=[dienstgrad, i.guild.get_role(cfg("rl_polizei") or 0)])
    desc = (f"Wir begrüßen unseren neuen Kollegen!\nDer Anwärter {user.mention} [LPN{lpn}] "
            f"wurde offiziell in den Polizeidienst aufgenommen.\n\n"
            f"• **Zugeteilter Dienstgrad:** {dienstgrad.mention}\n• **Einstellungsnotiz:** {notiz}\n\n"
            f"*Gezeichnet von der Direktion:* {person(i.user)}")
    await send_log(i, "ch_personal", embed("💫 Neueinstellung", desc, BLAU))


@bot.tree.command(description="Beförderung eintragen")
@only("rl_direktion")
async def uprank(i: discord.Interaction, user: discord.Member, dienstgrad: discord.Role,
                 begruendung: str, alter_dienstgrad: discord.Role | None = None):
    sperre = db["sperren"].get(str(user.id))
    if active(sperre):
        bis = dt.datetime.fromisoformat(sperre)
        return await i.response.send_message(f"❌ Uprank-Sperre bis {ts(bis)}.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    await change_roles(user, add=[dienstgrad], remove=[alter_dienstgrad])
    desc = (f"Dem Beamten {person(user)} wurde mit sofortiger Wirkung ein neuer Dienstgrad verliehen.\n\n"
            f"• **Neuer Dienstgrad:** {dienstgrad.mention}\n• **Begründung:** {begruendung}\n\n"
            f"*Gezeichnet von der Direktion:* {person(i.user)}")
    await send_log(i, "ch_personal", embed("🔼 Beförderung", desc, GRUEN))


@bot.tree.command(description="Degradierung eintragen")
@only("rl_direktion")
async def downrank(i: discord.Interaction, user: discord.Member, dienstgrad: discord.Role,
                   begruendung: str, alter_dienstgrad: discord.Role | None = None):
    await i.response.defer(ephemeral=True)
    await change_roles(user, add=[dienstgrad], remove=[alter_dienstgrad])
    desc = (f"Der Beamte {person(user)} wurde mit sofortiger Wirkung degradiert. "
            f"Der Vorfall wurde aktenkundig gemacht.\n\n"
            f"• **Neuer Dienstgrad:** {dienstgrad.mention}\n• **Begründung:** {begruendung}\n\n"
            f"*Gezeichnet von der Direktion:* {person(i.user)}")
    await send_log(i, "ch_personal", embed("🔽 Degradierung", desc, ORANGE))


@bot.tree.command(description="Dienstentlassung eintragen")
@only("rl_direktion")
async def entlassung(i: discord.Interaction, user: discord.Member, grund: str,
                     dienstgrad: discord.Role | None = None):
    await i.response.defer(ephemeral=True)
    desc = (f"Der Beamte {person(user)} wurde mit sofortiger Wirkung vom Dienst freigestellt "
            f"und aus dem Polizeiregister entfernt.\n\n• **Entlassungsgrund:** {grund}\n\n"
            f"*Gezeichnet von der Direktion:* {person(i.user)}")
    await change_roles(user, remove=[dienstgrad, i.guild.get_role(cfg("rl_polizei") or 0)])
    db["lpn"].pop(str(user.id), None)
    save()
    await send_log(i, "ch_personal", embed("❌ Dienstentlassung", desc, ROT))


@bot.tree.command(description="Disziplinarmaßnahme einleiten")
@only("rl_direktion")
async def disziplinar(i: discord.Interaction, user: discord.Member, vergehen: str,
                      laufzeit_tage: int = 7, uprank_sperre_tage: int = 0):
    await i.response.defer(ephemeral=True)
    az = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    bis = future(laufzeit_tage)
    db["disziplin"].append({"id": az, "user": user.id, "vergehen": vergehen, "bis": bis.isoformat()})
    text = vergehen
    if uprank_sperre_tage > 0:
        db["sperren"][str(user.id)] = future(uprank_sperre_tage).isoformat()
        text += f" + {uprank_sperre_tage} Tage Uprank-Sperre"
    save()
    desc = (f"Gegen den Beamten {person(user)} wurde ein offizieller Aktenvermerk erstellt.\n\n"
            f"• **Dienstvergehen:** {text}\n• **Aktenzeichen (ID):** `{az}`\n"
            f"• **Läuft automatisch ab:** {ts(bis)}\n\n*Gezeichnet von:* {person(i.user)}")
    await send_log(i, "ch_disziplin", embed("Disziplinarmaßnahme eingeleitet", desc, ROT))


@bot.tree.command(description="Aktive Akteneinträge eines Beamten anzeigen")
@only("rl_direktion")
async def akten(i: discord.Interaction, user: discord.Member):
    eintraege = [d for d in db["disziplin"] if d["user"] == user.id and active(d["bis"])]
    if not eintraege:
        return await i.response.send_message("Keine aktiven Einträge.", ephemeral=True)
    zeilen = [f"`{d['id']}` – {d['vergehen']} (bis {ts(dt.datetime.fromisoformat(d['bis']))})" for d in eintraege]
    await i.response.send_message("\n".join(zeilen)[:1900], ephemeral=True)


@bot.tree.command(description="Bewerbungssperre verhängen")
@only("rl_direktion")
async def bewerbungssperre(i: discord.Interaction, user: discord.User, grund: str, tage: int = 7):
    await i.response.defer(ephemeral=True)
    bis = future(tage)
    db["bewerbungssperren"][str(user.id)] = bis.isoformat()
    save()
    desc = (f"Gegen {user.mention} wurde eine Bewerbungsperre verhängt. Bewerbungen des Nutzers "
            f"werden für die angegebene Dauer nicht berücksichtigt.\n\n• **Grund:** {grund}\n"
            f"• **Dauer:** {tage}d (Bis: {ts(bis)})")
    await send_log(i, "ch_bewerbung", embed("⛔ Bewerbungsperre", desc, ROT))


@bot.tree.command(description="Bewerbungssperre aufheben")
@only("rl_direktion")
async def bewerbungssperre_aufheben(i: discord.Interaction, user: discord.User):
    db["bewerbungssperren"].pop(str(user.id), None)
    save()
    await i.response.send_message("✅ Sperre aufgehoben.", ephemeral=True)


# ---------- Anwalt ----------
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
    if msg.channel.id == cfg("ch_anwalt") and msg.content.strip().lower() == "anwalt":
        rolle = cfg("rl_pflichtverteidiger")
        if not rolle:
            return
        letzte = db["anwalt_cd"].get(str(msg.author.id), 0)
        rest = int(900 - (time.time() - letzte))
        if rest > 0:
            return await msg.reply(f"⏳ Bitte warte noch {rest // 60 + 1} Min.", delete_after=8)
        db["anwalt_cd"][str(msg.author.id)] = time.time()
        save()
        await msg.channel.send(f"<@&{rolle}> {msg.author.mention} benötigt einen Anwalt!",
                               allowed_mentions=discord.AllowedMentions(roles=True))


# ---------- Platzverweis ----------
class PlatzverweisModal(discord.ui.Modal, title="Platzverweis eintragen"):
    person_ = discord.ui.TextInput(label="Person (Roblox-Name)", max_length=40)
    ort = discord.ui.TextInput(label="Ort", max_length=80)
    grund = discord.ui.TextInput(label="Grund", style=discord.TextStyle.paragraph, max_length=500)
    dauer = discord.ui.TextInput(label="Dauer", default="24 Stunden", max_length=40)

    async def on_submit(self, i: discord.Interaction):
        desc = (f"• **Person:** {self.person_.value}\n• **Ort:** {self.ort.value}\n"
                f"• **Grund:** {self.grund.value}\n• **Dauer:** {self.dauer.value}\n\n"
                f"*Ausgestellt von:* {person(i.user)}")
        await i.channel.send(embed=embed("🚧 Platzverweis", desc, ORANGE))
        await i.response.send_message("✅ Platzverweis veröffentlicht.", ephemeral=True)


class PlatzverweisView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Platzverweis eintragen", emoji="🚧",
                       style=discord.ButtonStyle.primary, custom_id="platzverweis:open")
    async def open(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(PlatzverweisModal())


# ---------- Aktivitaetscheck ----------
class AktivitaetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Aktivität bestätigen", emoji="✅",
                       style=discord.ButtonStyle.success, custom_id="akt:confirm")
    async def confirm(self, i: discord.Interaction, b: discord.ui.Button):
        e = db["aktivitaet"].get(str(i.message.id))
        if not e or e["done"]:
            return await i.response.send_message("Dieser Check ist beendet.", ephemeral=True)
        if cfg("rl_polizei") and not has_any(i.user, "rl_polizei"):
            return await i.response.send_message("Nur für Polizisten.", ephemeral=True)
        if i.user.id not in e["confirmed"]:
            e["confirmed"].append(i.user.id)
            save()
        await i.response.send_message("✅ Aktivität bestätigt.", ephemeral=True)

    @discord.ui.button(label="Wer hat bestätigt?", emoji="📋",
                       style=discord.ButtonStyle.secondary, custom_id="akt:list")
    async def liste(self, i: discord.Interaction, b: discord.ui.Button):
        e = db["aktivitaet"].get(str(i.message.id))
        if not e:
            return await i.response.send_message("Check nicht gefunden.", ephemeral=True)
        ja = [f"<@{u}>" for u in e["confirmed"]]
        rolle = i.guild.get_role(cfg("rl_polizei") or 0)
        nein = [m.mention for m in (rolle.members if rolle else []) if m.id not in e["confirmed"]]
        text = (f"**✅ Bestätigt ({len(ja)}):**\n{', '.join(ja) or '–'}\n\n"
                f"**❌ Offen ({len(nein)}):**\n{', '.join(nein) or '–'}")
        await i.response.send_message(text[:1900], ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(description="Aktivitätscheck starten")
@only("rl_direktion")
async def aktivitaetscheck(i: discord.Interaction, stunden: int = 24):
    rolle = cfg("rl_polizei")
    if not rolle:
        return await i.response.send_message("❌ Erst /setup rolle → Polizei.", ephemeral=True)
    bis = now() + dt.timedelta(hours=stunden)
    desc = (f"Bitte bestätigt mit dem Button **Aktivität bestätigen** eure Aktivität.\n\n"
            f"⏳ **Frist:** {ts(bis)}\n\n⚠️ Wer bis dahin nicht bestätigt hat, erhält automatisch eine DAB Warn.")
    await i.response.send_message("✅ Check gestartet.", ephemeral=True)
    m = await i.channel.send(f"<@&{rolle}>", embed=embed("📢 Aktivitätscheck", desc, ORANGE),
                             view=AktivitaetView(),
                             allowed_mentions=discord.AllowedMentions(roles=True))
    db["aktivitaet"][str(m.id)] = {"guild": i.guild.id, "channel": i.channel.id,
                                   "deadline": bis.isoformat(), "confirmed": [], "done": False}
    save()


@tasks.loop(minutes=1)
async def checker():
    n = now()
    for mid, e in list(db["aktivitaet"].items()):
        if e["done"] or dt.datetime.fromisoformat(e["deadline"]) > n:
            continue
        e["done"] = True
        guild = bot.get_guild(e["guild"])
        rolle = guild.get_role(cfg("rl_polizei") or 0) if guild else None
        if not rolle:
            continue
        fehlend = [m for m in rolle.members if not m.bot and m.id not in e["confirmed"]]
        for m in fehlend:
            db["dab_warns"].setdefault(str(m.id), []).append(
                {"grund": "Aktivitätscheck nicht bestätigt", "datum": n.isoformat()})
        ch = guild.get_channel(cfg("ch_disziplin") or 0) or guild.get_channel(e["channel"])
        if ch:
            txt = ", ".join(m.mention for m in fehlend) or "Niemand 🎉"
            await ch.send(embed=embed("📢 Aktivitätscheck beendet",
                                      f"**DAB Warn erhalten ({len(fehlend)}):**\n{txt}"[:4000], ROT),
                          allowed_mentions=discord.AllowedMentions.none())
    if n.minute % 5 == 0:
        for g in bot.guilds:
            await refresh_teamliste(g)
    if bot.guilds:
        for uid, r in list(db["dienst"]["aktiv"].items()):
            if (n - dt.datetime.fromisoformat(r["start"])).total_seconds() > 12 * 3600:
                await dienst_beenden(bot.guilds[0], int(uid))  # vergessene Dienste nach 12 Std
    db["disziplin"] = [d for d in db["disziplin"] if active(d["bis"])]
    save()


@checker.before_loop
async def _wait():
    await bot.wait_until_ready()


# ---------- Team-Logs ----------
@bot.tree.command(description="Eintrag in einen Team-Log schreiben (z.B. Leitungsebene, Ausbilder)")
@app_commands.autocomplete(kategorie=kat_autocomplete)
async def teamlog(i: discord.Interaction, kategorie: str, titel: str, text: str):
    k = db["logkanaele"].get(kategorie)
    ch = i.guild.get_channel(k["channel"]) if k else None
    if not ch:
        return await i.response.send_message("❌ Unbekannte Log-Kategorie.", ephemeral=True)
    if k["rolle"]:
        erlaubt = i.user.guild_permissions.administrator or any(r.id == k["rolle"] for r in i.user.roles)
    else:
        erlaubt = has_any(i.user, "rl_polizei", "rl_direktion")
    if not erlaubt:
        return await i.response.send_message("❌ Dafür fehlt dir die Berechtigung.", ephemeral=True)
    desc = f"{text}\n\n• **Kategorie:** {kategorie}\n\n*Eingetragen von:* {person(i.user)}"
    await ch.send(embed=embed(f"📝 {titel}", desc, BLAU))
    await i.response.send_message("✅ Log eingetragen.", ephemeral=True)


# ---------- Fahndungen ----------
STUFEN = {"niedrig": ("🟡 Niedrig", ORANGE), "mittel": ("🟠 Mittel", ORANGE),
          "hoch": ("🔴 Hoch (gefährlich/bewaffnet)", ROT)}


class FahndungView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fahndung beenden", emoji="✅",
                       style=discord.ButtonStyle.success, custom_id="fahndung:end")
    async def beenden(self, i: discord.Interaction, b: discord.ui.Button):
        if not has_any(i.user, "rl_polizei", "rl_direktion"):
            return await i.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        f = db["fahndungen"].get(str(i.message.id))
        if not f or not f["aktiv"]:
            return await i.response.send_message("Fahndung nicht mehr aktiv.", ephemeral=True)
        f["aktiv"] = False
        save()
        e = i.message.embeds[0]
        e.title = "✅ Fahndung beendet"
        e.color = GRUEN
        e.description = e.description.replace("🔎 Aktiv", f"✅ Beendet durch {i.user.mention}")
        await i.response.edit_message(embed=e, view=None)


@bot.tree.command(description="Fahndung ausschreiben")
@only("rl_polizei", "rl_direktion")
@app_commands.choices(gefahr=[app_commands.Choice(name=v[0], value=k) for k, v in STUFEN.items()])
async def fahndung(i: discord.Interaction, gesuchte_person: str, grund: str,
                   gefahr: app_commands.Choice[str], beschreibung: str = "Keine Angabe",
                   zuletzt_gesehen: str = "Unbekannt"):
    await i.response.defer(ephemeral=True)
    label, farbe = STUFEN[gefahr.value]
    desc = (f"Nach folgender Person wird gefahndet:\n\n• **Person:** {gesuchte_person}\n"
            f"• **Grund:** {grund}\n• **Gefahrenstufe:** {label}\n• **Beschreibung:** {beschreibung}\n"
            f"• **Zuletzt gesehen:** {zuletzt_gesehen}\n• **Status:** 🔎 Aktiv\n\n"
            f"*Ausgeschrieben von:* {person(i.user)}")
    e = embed("🚨 Fahndung", desc, farbe)
    ch = i.guild.get_channel(cfg("ch_fahndung") or 0) or i.channel
    m = await ch.send(embed=e, view=FahndungView())
    db["fahndungen"][str(m.id)] = {"guild": i.guild.id, "channel": ch.id, "person": gesuchte_person,
                                   "aktiv": True, "von": i.user.id}
    save()
    await mirror(i.guild, "fahndung", e, ch.id)
    await i.followup.send("✅ Fahndung ausgeschrieben.", ephemeral=True)


@bot.tree.command(description="Aktive Fahndungen anzeigen")
@only("rl_polizei", "rl_direktion")
async def fahndungen(i: discord.Interaction):
    zeilen = [f"• {f['person']} – https://discord.com/channels/{f['guild']}/{f['channel']}/{mid}"
              for mid, f in db["fahndungen"].items() if f["aktiv"] and f["guild"] == i.guild.id]
    await i.response.send_message("\n".join(zeilen)[:1900] or "Keine aktiven Fahndungen.", ephemeral=True)


# ---------- Dienstsystem ----------
START = now()


def fmt_dauer(sek):
    h, r = divmod(int(sek), 3600)
    return f"{h} Std {r // 60} Min"


def dienst_sekunden(r):
    n = now()
    gesamt = (n - dt.datetime.fromisoformat(r["start"])).total_seconds() - r.get("pause_total", 0)
    if r.get("pause_start"):
        gesamt -= (n - dt.datetime.fromisoformat(r["pause_start"])).total_seconds()
    return max(0, gesamt)


def dienst_embed(guild):
    zeilen = []
    for uid, r in db["dienst"]["aktiv"].items():
        m = guild.get_member(int(uid))
        name = person(m) if m else f"<@{uid}>"
        status = "⏸️ Pause" if r.get("pause_start") else "🟢 Im Dienst"
        seit = discord.utils.format_dt(dt.datetime.fromisoformat(r["start"]), "R")
        zeilen.append(f"{status} · {name} · seit {seit}")
    liste = "\n".join(zeilen)[:3500] or "Niemand"
    desc = ("Nutze die Buttons, um deinen Dienst zu **beginnen**, zu **pausieren** oder zu **beenden**.\n\n"
            f"**Aktuell im Dienst ({len(zeilen)}):**\n{liste}")
    return embed("🚓 Dienstpanel", desc, GRUEN, panel=True)


async def refresh_dienst_panel(guild):
    p = db["dienst"].get("panel")
    ch = guild.get_channel(p["channel"]) if p else None
    if not ch:
        return
    try:
        msg = await ch.fetch_message(p["message"])
        await msg.edit(embed=dienst_embed(guild))
    except discord.HTTPException:
        pass


async def dienst_beenden(guild, member_id, durch=None):
    r = db["dienst"]["aktiv"].pop(str(member_id), None)
    if not r:
        return None
    sek = dienst_sekunden(r)
    gesamt = db["dienst"]["gesamt"].get(str(member_id), 0) + sek
    db["dienst"]["gesamt"][str(member_id)] = gesamt
    save()
    m = guild.get_member(member_id)
    if m:
        await change_roles(m, remove=[guild.get_role(cfg("rl_imdienst") or 0)])
    await refresh_dienst_panel(guild)
    name = person(m) if m else f"<@{member_id}>"
    extra = f"\n*Beendet durch:* {durch.mention}" if durch else ""
    e = embed("🔴 Dienst beendet",
              f"{name}\n• **Dauer:** {fmt_dauer(sek)}\n• **Gesamt:** {fmt_dauer(gesamt)}{extra}", ROT)
    ch = guild.get_channel(cfg("ch_dienst") or 0)
    if ch:
        await ch.send(embed=e)
    await mirror(guild, "dienst", e, ch.id if ch else None)
    return sek


class DienstView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Dienst antreten", emoji="🟢", style=discord.ButtonStyle.success, custom_id="dienst:start")
    async def antreten(self, i: discord.Interaction, b: discord.ui.Button):
        if not has_any(i.user, "rl_polizei", "rl_direktion"):
            return await i.response.send_message("❌ Nur für Polizisten.", ephemeral=True)
        akt = db["dienst"]["aktiv"]
        if str(i.user.id) in akt:
            return await i.response.send_message("Du bist bereits im Dienst.", ephemeral=True)
        akt[str(i.user.id)] = {"start": now().isoformat(), "pause_total": 0, "pause_start": None}
        save()
        await i.response.defer(ephemeral=True)
        await change_roles(i.user, add=[i.guild.get_role(cfg("rl_imdienst") or 0)])
        await refresh_dienst_panel(i.guild)
        await i.followup.send("🟢 Du bist jetzt im Dienst. Viel Erfolg!", ephemeral=True)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="dienst:pause")
    async def pause(self, i: discord.Interaction, b: discord.ui.Button):
        r = db["dienst"]["aktiv"].get(str(i.user.id))
        if not r:
            return await i.response.send_message("Du bist nicht im Dienst.", ephemeral=True)
        if r.get("pause_start"):
            r["pause_total"] += (now() - dt.datetime.fromisoformat(r["pause_start"])).total_seconds()
            r["pause_start"] = None
            text = "▶️ Pause beendet."
        else:
            r["pause_start"] = now().isoformat()
            text = "⏸️ Pause gestartet. Die Pausenzeit zählt nicht zur Dienstzeit."
        save()
        await i.response.defer(ephemeral=True)
        await refresh_dienst_panel(i.guild)
        await i.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Dienst beenden", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="dienst:end")
    async def beenden(self, i: discord.Interaction, b: discord.ui.Button):
        if str(i.user.id) not in db["dienst"]["aktiv"]:
            return await i.response.send_message("Du bist nicht im Dienst.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        sek = await dienst_beenden(i.guild, i.user.id)
        await i.followup.send(f"🔴 Dienst beendet. Dauer: **{fmt_dauer(sek)}**", ephemeral=True)


@bot.tree.command(description="Dienstzeit anzeigen")
async def dienstzeit(i: discord.Interaction, user: discord.Member | None = None):
    z = user or i.user
    sek = db["dienst"]["gesamt"].get(str(z.id), 0)
    r = db["dienst"]["aktiv"].get(str(z.id))
    if r:
        sek += dienst_sekunden(r)
    desc = f"{person(z)}\n• **Gesamt:** {fmt_dauer(sek)}" + ("\n• 🟢 Aktuell im Dienst" if r else "")
    await i.response.send_message(embed=embed("⏱️ Dienstzeit", desc), ephemeral=True)


@bot.tree.command(description="Top-10 der Dienstzeiten")
@only("rl_polizei", "rl_direktion")
async def dienst_topliste(i: discord.Interaction):
    top = sorted(db["dienst"]["gesamt"].items(), key=lambda kv: kv[1], reverse=True)[:10]
    zeilen = [f"**{n}.** <@{uid}> – {fmt_dauer(s)}" for n, (uid, s) in enumerate(top, 1)]
    await i.response.send_message(embed=embed("🏆 Top Dienstzeiten", "\n".join(zeilen) or "Noch keine Daten.", ORANGE),
                                  allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(description="Dienst eines Beamten zwangsweise beenden")
@only("rl_direktion")
async def dienst_zwangsbeenden(i: discord.Interaction, user: discord.Member):
    await i.response.defer(ephemeral=True)
    sek = await dienst_beenden(i.guild, user.id, i.user)
    await i.followup.send("✅ Dienst beendet." if sek is not None else "Der Beamte ist nicht im Dienst.", ephemeral=True)


# ---------- Teamliste ----------
def teamliste_embed(guild):
    raenge = [guild.get_role(r) for r in db["config"].get("raenge", [])]
    gesehen, total = set(), 0
    e = embed("📋 Teamliste", "", panel=True)
    for r in (x for x in raenge if x):
        mem = [m for m in r.members if not m.bot and m.id not in gesehen]
        if not mem:
            continue
        gesehen.update(m.id for m in mem)
        total += len(mem)
        mem.sort(key=lambda m: db["lpn"].get(str(m.id), 9999))
        e.add_field(name=f"{r.name} ({len(mem)})", value="\n".join(person(m) for m in mem)[:1020], inline=False)
    e.description = f"**{total}** Beamte im Dienstregister"
    return e


async def refresh_teamliste(guild):
    p = db["config"].get("teamliste")
    ch = guild.get_channel(p["channel"]) if p else None
    if not ch:
        return
    try:
        msg = await ch.fetch_message(p["message"])
        await msg.edit(embed=teamliste_embed(guild))
    except discord.HTTPException:
        pass


@bot.tree.command(description="Teamliste senden (aktualisiert sich automatisch)")
@only("rl_direktion")
async def teamliste(i: discord.Interaction):
    if not db["config"].get("raenge"):
        return await i.response.send_message("❌ Erst Ränge festlegen: /setup rang_hinzufuegen", ephemeral=True)
    await i.response.send_message("✅ Teamliste gesendet.", ephemeral=True)
    m = await i.channel.send(embed=teamliste_embed(i.guild))
    db["config"]["teamliste"] = {"channel": i.channel.id, "message": m.id}
    save()


# ---------- Hilfe & Status ----------
HILFE = {
    "🚀 Erste Schritte": [
        ("/setup status", "Zeigt, was eingerichtet ist und was noch fehlt"),
        ("/setup rolle", "Direktion, Polizei, Pflichtverteidiger, Ausweis-Team, Im-Dienst festlegen"),
        ("/setup kanal", "Personal-, Disziplin-, Bewerbungs-, Ausweis-, Fahndungs- und Dienst-Kanal"),
        ("/setup logkanal", "Eigene Log-Kategorien anlegen (z.B. Leitungsebene, Ausbilder)"),
        ("/setup rang_hinzufuegen", "Dienstgrade für die Teamliste festlegen (höchster zuerst)"),
        ("/setup logo · banner · farbe", "Design der Embeds und Panels anpassen"),
        ("/panel …", "Panels senden: dienst, anwalt, platzverweis, ausweis"),
    ],
    "👮 Personal 🔒": [
        ("/einstellung", "Neueinstellung mit LPN und Dienstgrad"),
        ("/uprank", "Beförderung (prüft Uprank-Sperren)"),
        ("/downrank", "Degradierung"),
        ("/entlassung", "Dienstentlassung, entfernt Polizei-Rolle"),
        ("/disziplinar", "Aktenvermerk mit Aktenzeichen, Ablauf und optionaler Uprank-Sperre"),
        ("/akten", "Aktive Akteneinträge eines Beamten"),
        ("/bewerbungssperre", "Bewerbungssperre verhängen (/bewerbungssperre_aufheben hebt sie auf)"),
        ("/aktivitaetscheck", "Check mit Button; wer nicht bestätigt, bekommt eine DAB-Warnung"),
    ],
    "🚓 Dienst": [
        ("/panel dienst 🔒", "Dienstpanel mit Antreten, Pause und Beenden"),
        ("/dienstzeit", "Deine Dienstzeit oder die eines anderen"),
        ("/dienst_topliste", "Top-10 der Dienstzeiten"),
        ("/dienst_zwangsbeenden 🔒", "Hängengebliebenen Dienst beenden"),
        ("/teamliste 🔒", "Teamliste nach Rängen, aktualisiert sich selbst"),
    ],
    "🚨 Fahndung & Einsatz": [
        ("/fahndung", "Fahndung ausschreiben (mit Gefahrenstufe)"),
        ("/fahndungen", "Alle aktiven Fahndungen mit Link"),
        ("/panel platzverweis 🔒", "Panel mit Button für Platzverweise"),
        ("/panel anwalt 🔒", "Anwalt-Panel: Wort „Anwalt“ pingt die Pflichtverteidiger (15 Min Cooldown)"),
    ],
    "🪪 Ausweise": [
        ("/panel ausweis 🔒", "Antragspanel mit Kategorie-Auswahl"),
        ("/personalausweis get", "Zeigt deinen Ausweis (oder den eines anderen)"),
        ("/ausweis_lizenzen", "Führerscheine und Waffenscheine eintragen (Ausweis-Team)"),
    ],
    "📝 Logs": [
        ("/teamlog", "Eintrag in eine Log-Kategorie schreiben"),
        ("/setup auto_log 🔒", "Ereignisse automatisch in eine Kategorie kopieren"),
        ("/status", "Ping, Laufzeit und Zahlen des Bots"),
    ],
}


class HilfeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Kategorie wählen…", options=[discord.SelectOption(label=k) for k in HILFE])

    async def callback(self, i: discord.Interaction):
        k = self.values[0]
        text = "\n".join(f"`{c}` – {t}" for c, t in HILFE[k])
        await i.response.edit_message(embed=embed(k, text), view=self.view)


class HilfeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(HilfeSelect())


@bot.tree.command(description="Alle Befehle und Funktionen erklärt")
async def hilfe(i: discord.Interaction):
    desc = ("Wähle unten eine Kategorie aus.\n🔒 = nur Direktion/Admin\n\n"
            "**Neu hier?** Starte mit `/setup status`.")
    await i.response.send_message(embed=embed("📖 Hilfe", desc, panel=True), view=HilfeView(), ephemeral=True)


@bot.tree.command(description="Bot-Status und Zahlen")
async def status(i: discord.Interaction):
    aktiv_f = sum(1 for f in db["fahndungen"].values() if f["aktiv"])
    desc = (f"• **Ping:** {round(bot.latency * 1000)} ms\n"
            f"• **Laufzeit:** {fmt_dauer((now() - START).total_seconds())}\n"
            f"• **Beamte im Register:** {len(db['lpn'])}\n"
            f"• **Im Dienst:** {len(db['dienst']['aktiv'])}\n"
            f"• **Aktive Fahndungen:** {aktiv_f}\n"
            f"• **Ausweise:** {len(db['ausweise'])}")
    await i.response.send_message(embed=embed("📊 Bot-Status", desc), ephemeral=True)


# ---------- Panels ----------
panel = app_commands.Group(name="panel", description="Panels senden")


@panel.command(name="anwalt", description="Anwalt-Panel senden (dieser Kanal wird Anwalt-Kanal)")
@only("rl_direktion")
async def panel_anwalt(i: discord.Interaction):
    rolle = cfg("rl_pflichtverteidiger")
    if not rolle:
        return await i.response.send_message("❌ Erst /setup rolle → Pflichtverteidiger.", ephemeral=True)
    db["config"]["ch_anwalt"] = i.channel.id
    save()
    desc = (f'Schreiben Sie einfach das Wort **"Anwalt"** in diesen Kanal, um die Rolle <@&{rolle}> '
            f"zu alarmieren und einen Anwalt anzufordern.\n\n*Pro Nutzer ist dies nur alle 15 Minuten möglich.*")
    await i.channel.send(embed=embed("👔 Anwalt benötigt?", desc, panel=True))
    await i.response.send_message("✅ Gesendet.", ephemeral=True)


@panel.command(name="platzverweis", description="Platzverweis-Panel senden")
@only("rl_direktion")
async def panel_platzverweis(i: discord.Interaction):
    desc = ("Betätigen Sie den Button, um einen Platzverweis für eine Person zu erfassen. "
            "Die Angaben werden anschließend automatisch in diesem Kanal veröffentlicht.")
    await i.channel.send(embed=embed("🚧 Platzverweis ausstellen", desc, panel=True), view=PlatzverweisView())
    await i.response.send_message("✅ Gesendet.", ephemeral=True)


@panel.command(name="ausweis", description="Ausweis-Antragspanel senden")
@only("rl_direktion")
async def panel_ausweis(i: discord.Interaction):
    await i.channel.send(embed=embed("🪪 Antragssystem", "Wähle unten im Menü aus, was du beantragen möchtest.", panel=True),
                         view=AusweisPanelView())
    await i.response.send_message("✅ Gesendet.", ephemeral=True)


@panel.command(name="dienst", description="Dienstpanel senden")
@only("rl_direktion")
async def panel_dienst(i: discord.Interaction):
    await i.response.send_message("✅ Gesendet.", ephemeral=True)
    m = await i.channel.send(embed=dienst_embed(i.guild), view=DienstView())
    db["dienst"]["panel"] = {"channel": i.channel.id, "message": m.id}
    save()


bot.tree.add_command(panel)


# ---------- Ausweissystem ----------
class AusweisModal(discord.ui.Modal, title="Personalausweis"):
    rp_name = discord.ui.TextInput(label="Vor- und Nachname (IC)", max_length=40)
    geb = discord.ui.TextInput(label="Geburtsdatum (TT.MM.JJJJ)", max_length=10)
    roblox = discord.ui.TextInput(label="Roblox-Username", max_length=30)

    async def on_submit(self, i: discord.Interaction):
        try:
            dt.datetime.strptime(self.geb.value.strip(), "%d.%m.%Y")
        except ValueError:
            return await i.response.send_message("❌ Datum bitte als TT.MM.JJJJ.", ephemeral=True)
        ch = i.guild.get_channel(cfg("ch_ausweis_pruefung") or 0)
        if not ch:
            return await i.response.send_message("❌ Prüf-Kanal ist nicht eingerichtet.", ephemeral=True)
        daten = {"user": i.user.id, "name": self.rp_name.value.strip(),
                 "geb": self.geb.value.strip(), "roblox": self.roblox.value.strip().lstrip("@")}
        desc = (f"**Antragsteller:** {i.user.mention}\n**Name:** {daten['name']}\n"
                f"**Geburtsdatum:** {daten['geb']}\n**Roblox:** @{daten['roblox']}")
        m = await ch.send(embed=embed("🪪 Ausweisantrag", desc), view=AusweisReviewView())
        db["ausweis_anfragen"][str(m.id)] = daten
        save()
        await i.response.send_message("✅ Dein Antrag wurde eingereicht.", ephemeral=True)


class AusweisPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(placeholder="Wähle eine Kategorie...", custom_id="ausweis:select",
                       options=[discord.SelectOption(label="Personalausweis beantragen", emoji="🪪", value="neu"),
                                discord.SelectOption(label="Personalausweis ändern", emoji="✏️", value="edit")])
    async def sel(self, i: discord.Interaction, s: discord.ui.Select):
        await i.response.send_modal(AusweisModal())
        try:
            await i.message.edit(view=AusweisPanelView())  # Auswahl zuruecksetzen
        except discord.HTTPException:
            pass


class AusweisReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle(self, i: discord.Interaction, ok: bool):
        if not has_any(i.user, "rl_ausweis_team", "rl_direktion"):
            return await i.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        req = db["ausweis_anfragen"].pop(str(i.message.id), None)
        if not req:
            return await i.response.send_message("❌ Antrag nicht mehr vorhanden.", ephemeral=True)
        if ok:
            alt = db["ausweise"].get(str(req["user"]), {})
            db["ausweise"][str(req["user"])] = {
                "name": req["name"], "geb": req["geb"], "roblox": req["roblox"],
                "fuehrerscheine": alt.get("fuehrerscheine", "Keine"),
                "gross": alt.get("gross", False), "klein": alt.get("klein", False)}
        save()
        e = i.message.embeds[0]
        e.color = GRUEN if ok else ROT
        e.add_field(name="Status", value=f"{'✅ Angenommen' if ok else '❌ Abgelehnt'} von {i.user.mention}")
        await i.response.edit_message(embed=e, view=None)
        member = i.guild.get_member(req["user"])
        if member:
            try:
                await member.send("✅ Dein Ausweis wurde ausgestellt! Zeige ihn mit /personalausweis get."
                                  if ok else "❌ Dein Ausweisantrag wurde abgelehnt.")
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Annehmen", style=discord.ButtonStyle.success, custom_id="ausweis:ok")
    async def ok(self, i: discord.Interaction, b: discord.ui.Button):
        await self._handle(i, True)

    @discord.ui.button(label="Ablehnen", style=discord.ButtonStyle.danger, custom_id="ausweis:no")
    async def no(self, i: discord.Interaction, b: discord.ui.Button):
        await self._handle(i, False)


@bot.tree.command(description="Führerscheine & Waffenscheine eines Ausweises setzen")
@only("rl_ausweis_team", "rl_direktion")
async def ausweis_lizenzen(i: discord.Interaction, user: discord.Member, fuehrerscheine: str,
                           grosser_waffenschein: bool = False, kleiner_waffenschein: bool = False):
    a = db["ausweise"].get(str(user.id))
    if not a:
        return await i.response.send_message("❌ Der Nutzer hat keinen Ausweis.", ephemeral=True)
    a.update({"fuehrerscheine": fuehrerscheine, "gross": grosser_waffenschein, "klein": kleiner_waffenschein})
    save()
    await i.response.send_message("✅ Aktualisiert.", ephemeral=True)


def font(size):
    for p in ("fonts/font.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def render_card(a, user_id, avatar_bytes):
    W, H = 1000, 600
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for x in range(W):
        t = x / W
        d.line([(x, 0), (x, H)], fill=(int(228 - 40 * t), int(206 - 25 * t), 250, 255))
    d.text((50, 35), "Personalausweis", font=font(46), fill=(15, 15, 15))
    d.text((W - 50, 25), "Nova", font=font(72), fill=(110, 50, 220), anchor="ra")
    d.line([(50, 115), (W - 50, 115)], fill=(90, 40, 190), width=5)
    zeilen = [f"Name: {a['name']}", f"Geburtsdatum: {a['geb']}",
              f"Führerscheine: {a['fuehrerscheine']}",
              f"Großer Waffenschein: {'Ja' if a['gross'] else 'Nein'}",
              f"Kleiner Waffenschein: {'Ja' if a['klein'] else 'Nein'}"]
    y = 150
    for z in zeilen:
        d.text((55, y), z, font=font(30), fill=(10, 10, 10))
        y += 58
    rnd = random.Random(int(user_id))
    x = 55
    while x < 600:
        w = rnd.choice((3, 4, 6, 8))
        d.rectangle((x, 460, x + w, 565), fill=(0, 0, 0))
        x += w + rnd.choice((3, 4, 6))
    box = (690, 160, 940, 410)
    try:
        av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((250, 250))
        img.paste(av, box[:2], av)
    except Exception:
        d.rectangle(box, fill=(60, 60, 60))
    d.text((815, 430), f"Roblox: @{a['roblox']}", font=font(26), fill=(10, 10, 10), anchor="ma")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W, H), radius=40, fill=255)
    img.putalpha(mask)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


async def roblox_headshot(username):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://users.roblox.com/v1/usernames/users",
                              json={"usernames": [username], "excludeBannedUsers": False}) as r:
                uid = (await r.json())["data"][0]["id"]
            async with s.get("https://thumbnails.roblox.com/v1/users/avatar-headshot",
                             params={"userIds": uid, "size": "150x150", "format": "Png"}) as r:
                url = (await r.json())["data"][0]["imageUrl"]
            async with s.get(url) as r:
                return await r.read()
    except Exception:
        return None


personalausweis = app_commands.Group(name="personalausweis", description="Personalausweis")


@personalausweis.command(name="get", description="Personalausweis anzeigen")
async def pa_get(i: discord.Interaction, user: discord.Member | None = None):
    ziel = user or i.user
    a = db["ausweise"].get(str(ziel.id))
    if not a:
        return await i.response.send_message("❌ Es gibt keinen Ausweis für diesen Nutzer.", ephemeral=True)
    await i.response.defer()
    avatar = await roblox_headshot(a["roblox"])
    buf = render_card(a, ziel.id, avatar)
    await i.followup.send(file=discord.File(buf, "personalausweis.png"))


bot.tree.add_command(personalausweis)


@bot.event
async def on_ready():
    print(f"Eingeloggt als {bot.user}")


if __name__ == "__main__":
    bot.run(TOKEN)

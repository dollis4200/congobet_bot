import asyncio
import json
import logging
import math
import os
import re
from pathlib import Path
from datetime import datetime, timezone

from playwright.async_api import async_playwright

URL_INSTANT_LEAGUE = "https://www.congobet.net/virtual/category/instant-league/8035/matches"
URL_RESULTATS = "https://www.congobet.net/virtual/category/instant-league/8035/results"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("congobet-bot")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "oui", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def env_int(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


CONFIG = {
    "mise_base": env_float("MISE_BASE", 50.0),
    "multiplicateur_recuperation": env_float("MULTIPLICATEUR_RECUPERATION", 1.25),
    "mode_pari": os.getenv("MODE_PARI", "oui"),
    "analyse_tous_les_n_cycles": env_int("ANALYSE_TOUS_LES_N_CYCLES", 5),
    "pause_recherche_sec": env_int("PAUSE_RECHERCHE_SEC", 15),
    "pause_apres_pari_sec": env_int("PAUSE_APRES_PARI_SEC", 5),
    "pause_resultats_sec": env_int("PAUSE_RESULTATS_SEC", 25),
    "timeout_matchs_ms": env_int("TIMEOUT_MATCHS_MS", 45000),
    "taille_rapport": env_int("TAILLE_RAPPORT", 20),
    "reset_apres_defaites": env_int("RESET_APRES_DEFAITES", 3),
    "max_defaites_session": env_int("MAX_DEFAITES_SESSION", 6),
    "stop_loss_pct": env_float("STOP_LOSS_PCT", 15.0),
    "max_engagement_pct": env_float("MAX_ENGAGEMENT_PCT", 5.0),
    "max_mise_multiple_base": env_float("MAX_MISE_MULTIPLE_BASE", 3.0),
    "min_cote": env_float("MIN_COTE", 1.15),
    "max_cote": env_float("MAX_COTE", 3.50),
    "min_sample_resultats": env_int("MIN_SAMPLE_RESULTATS", 6),
    "max_cycles": env_int("MAX_CYCLES", None),
    "headless": env_bool("HEADLESS", True),
    "equipe_cible_defaut": os.getenv("EQUIPE_CIBLE_DEFAUT", "manchester blue"),
    "mots_cles_marche_gng": [
        "g/ng", "gg/ng", "g.ng", "g-ng",
        "les deux equipes marquent", "les deux équipes marquent",
        "deux equipes marquent", "deux équipes marquent",
        "btts", "but/pas de but", "but - pas de but"
    ],
    "messages_confirmation_pari": [
        "pari placé", "parié", "bet placed", "accepted", "accepté", "enregistré"
    ],
}


def lire_identifiants():
    identifiant = os.getenv("CONGOBET_ID", "").strip()
    mot_de_passe = os.getenv("CONGOBET_PASSWORD", "").strip()
    if not identifiant or not mot_de_passe:
        raise RuntimeError(
            "Variables d'environnement manquantes: CONGOBET_ID et/ou CONGOBET_PASSWORD."
        )
    return identifiant, mot_de_passe


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def normaliser_nom_equipe(texte):
    return clean_text(texte)


def parse_money(text):
    if not text:
        return None
    s = re.sub(r"[^\d,.-]", "", str(text)).strip()
    if not s:
        return None

    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    if last_dot != -1 and last_comma != -1:
        if last_dot > last_comma:
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif s.count(",") > 1 and "." not in s:
        s = s.replace(",", "")
    elif s.count(".") > 1 and "," not in s:
        s = s.replace(".", "")
    else:
        s = s.replace(",", ".")

    try:
        return float(s)
    except Exception:
        return None


def extraire_hhmm(texte):
    if not texte:
        return None
    m = re.search(r"(\d{2}:\d{2})", str(texte))
    return m.group(1) if m else None


def hhmm_vers_minutes(hhmm):
    if not hhmm:
        return None
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def soustraire_minutes(hhmm, nb_min=2):
    total = hhmm_vers_minutes(hhmm)
    if total is None:
        return None
    total = (total - nb_min) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def comparer_hhmm(h1, h2):
    m1 = hhmm_vers_minutes(h1)
    m2 = hhmm_vers_minutes(h2)
    if m1 is None or m2 is None:
        return None
    diff = (m1 - m2) % (24 * 60)
    if diff == 0:
        return 0
    return 1 if diff < 720 else -1


def clip(val, lo, hi):
    return max(lo, min(hi, val))


def calculer_mise_recuperation(perte_cumulee, cote, bankroll_courante):
    base = CONFIG["mise_base"]
    if not cote or cote <= 1.0 or perte_cumulee <= 0:
        return base
    brute = (perte_cumulee * CONFIG["multiplicateur_recuperation"]) / (cote - 1.0)
    plafond_bankroll = bankroll_courante * (CONFIG["max_engagement_pct"] / 100.0) if bankroll_courante else base
    plafond_base = base * CONFIG["max_mise_multiple_base"]
    return round(clip(math.ceil(brute), base, max(base, min(plafond_bankroll, plafond_base))), 2)


def score_equipe(oui, total, alpha=1.0, beta=1.0):
    if total <= 0:
        return 0.0
    taux = (oui + alpha) / (total + alpha + beta)
    bonus_volume = min(total, 12) / 12 * 0.10
    return taux + bonus_volume


def make_opportunity_key(opportunite):
    return "|".join([
        str(opportunite.get("round_index")),
        str(opportunite.get("heure_round")),
        normaliser_nom_equipe(opportunite.get("equipe_dom")),
        normaliser_nom_equipe(opportunite.get("equipe_ext")),
    ])


async def safe_click(locator, timeout=8000):
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(timeout=timeout)
        return True
    except Exception:
        return False


async def safe_fill(locator, value, timeout=8000):
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(timeout=timeout)
        await locator.fill("")
        await locator.fill(str(value), timeout=timeout)
        return True
    except Exception:
        return False


async def initialiser_navigateur(url_cible):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=CONFIG["headless"])
    context = await browser.new_context(
        locale="fr-FR",
        viewport={"width": 1600, "height": 2400},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    page.set_default_timeout(30000)
    await page.goto(url_cible, wait_until="domcontentloaded", timeout=120000)
    await page.wait_for_timeout(2500)
    return pw, browser, context, page


async def fermer_session(pw=None, browser=None, context=None):
    try:
        if context:
            await context.close()
    except Exception:
        pass
    try:
        if browser:
            await browser.close()
    except Exception:
        pass
    try:
        if pw:
            await pw.stop()
    except Exception:
        pass


async def lire_bankroll(page):
    for sel in ["#Currency_Value", "[id*='Currency']", ".currency-value"]:
        try:
            locator = page.locator(sel).first
            if await locator.count() and await locator.is_visible():
                return parse_money(await locator.text_content())
        except Exception:
            continue
    return None


async def reinitialiser_et_se_connecter(page, url_cible, identifiant, mot_de_passe):
    await page.goto(url_cible, wait_until="domcontentloaded", timeout=120000)
    await page.wait_for_timeout(2500)

    bankroll = await lire_bankroll(page)
    if bankroll is not None:
        logger.info("Session déjà authentifiée. Bankroll détectée: %s", bankroll)
        return True

    candidats_btn = [
        page.locator("#Header_Login_Button").first,
        page.locator("button", has_text=re.compile(r"connexion|se connecter", re.I)).first,
        page.locator(".header-login-button").first,
    ]
    btn_ok = False
    for loc in candidats_btn:
        try:
            if await loc.count() and await safe_click(loc, timeout=4000):
                btn_ok = True
                break
        except Exception:
            continue

    if not btn_ok:
        logger.error("Bouton de connexion introuvable.")
        return False

    await page.wait_for_timeout(1500)

    id_loc = page.locator("#Login_Id, hg-message #Login_Id").first
    pwd_loc = page.locator("#Login_Password, hg-message #Login_Password").first
    submit_loc = page.locator("#Login_Button, hg-message #Login_Button").first

    if not await safe_fill(id_loc, identifiant):
        logger.error("Champ identifiant inaccessible.")
        return False

    if not await safe_fill(pwd_loc, mot_de_passe):
        try:
            await page.evaluate("""() => {
                const el = document.querySelector('#Login_Password') || document.querySelector('hg-message #Login_Password');
                if (el) el.removeAttribute('readonly');
            }""")
            if not await safe_fill(pwd_loc, mot_de_passe):
                logger.error("Champ mot de passe inaccessible.")
                return False
        except Exception:
            logger.error("Champ mot de passe inaccessible.")
            return False

    if not await safe_click(submit_loc, timeout=8000):
        logger.error("Bouton de validation inaccessible.")
        return False

    await page.wait_for_timeout(5000)
    bankroll = await lire_bankroll(page)
    ok = bankroll is not None
    if ok:
        logger.info("Connexion confirmée.")
    else:
        logger.error("Connexion non confirmée.")
    return ok


async def wait_for_matches(page, minimum=1, timeout_ms=None):
    timeout_ms = timeout_ms or CONFIG["timeout_matchs_ms"]
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    selecteurs = ["div.match", "hg-instant-league-matches div.match", "hg-event-list-item"]
    while asyncio.get_event_loop().time() < deadline:
        try:
            count = 0
            for sel in selecteurs:
                try:
                    count = max(count, await page.locator(sel).count())
                except Exception:
                    pass
            if count >= minimum:
                return count
        except Exception:
            pass
        await page.wait_for_timeout(500)
    raise TimeoutError("Les matchs ne se sont pas chargés à temps.")


async def ensure_gng_selected(page):
    active = page.locator("hg-event-bet-type-picker button.active").first
    try:
        if await active.count():
            txt = clean_text(await active.text_content())
            if any(k in txt for k in CONFIG["mots_cles_marche_gng"]):
                return True
    except Exception:
        pass

    boutons = page.locator("hg-event-bet-type-picker button")
    for i in range(await boutons.count()):
        btn = boutons.nth(i)
        try:
            txt = clean_text(await btn.text_content())
            if any(k in txt for k in CONFIG["mots_cles_marche_gng"]):
                if await safe_click(btn):
                    await page.wait_for_timeout(1200)
                    return True
        except Exception:
            continue

    select_box = page.locator("hg-event-bet-type-picker hg-select .selected").first
    if await select_box.count() and await safe_click(select_box, timeout=5000):
        options = page.locator("hg-event-bet-type-picker hg-select .dropdown .option")
        for i in range(await options.count()):
            opt = options.nth(i)
            try:
                txt = clean_text(await opt.text_content())
                if any(k in txt for k in CONFIG["mots_cles_marche_gng"]):
                    if await safe_click(opt, timeout=5000):
                        await page.wait_for_timeout(1200)
                        return True
            except Exception:
                continue

    raise RuntimeError("Impossible de sélectionner le marché G/NG.")


async def lire_heures_onglets(page):
    s_rounds = "hg-instant-league-round-picker li"
    n = await page.locator(s_rounds).count()
    heures = []
    for i in range(n):
        brut = ((await page.locator(s_rounds).nth(i).text_content()) or "").strip()
        if not brut or "live" in brut.lower():
            heures.append(None)
        elif i == 0:
            heures.append(None)
        else:
            heures.append(extraire_hhmm(brut))
    if len(heures) >= 2 and heures[1] is not None:
        heures[0] = soustraire_minutes(heures[1], 2)
    return heures


async def localiser_marche_gng(match_element):
    odds = match_element.locator("hg-event-bet-type-item .odds")
    try:
        n = await odds.count()
    except Exception:
        n = 0
    if n >= 2:
        try:
            cote_oui = parse_money(await odds.nth(0).text_content())
            cote_non = parse_money(await odds.nth(1).text_content())
            if cote_oui and cote_non:
                return {
                    "cote_oui": cote_oui,
                    "cote_non": cote_non,
                    "el_oui": odds.nth(0),
                    "el_non": odds.nth(1),
                }
        except Exception:
            pass
    return None


async def extraire_matchs_round_courant(page):
    matchs = []
    blocs = page.locator("div.match")
    for i in range(await blocs.count()):
        bloc = blocs.nth(i)
        try:
            equipes = bloc.locator(".teams span")
            if await equipes.count() < 2:
                continue
            eq_dom = ((await equipes.nth(0).text_content()) or "").strip()
            eq_ext = ((await equipes.nth(1).text_content()) or "").strip()
            marche = await localiser_marche_gng(bloc)
            if not marche:
                continue
            matchs.append({
                "equipe_dom": eq_dom,
                "equipe_ext": eq_ext,
                **marche,
            })
        except Exception:
            continue
    return matchs


async def trouver_match_du_cycle(page, url_cible, equipe_active, opportunites_traitees):
    eq_norm = normaliser_nom_equipe(equipe_active)
    await page.goto(url_cible, wait_until="domcontentloaded", timeout=120000)
    await page.wait_for_timeout(3000)
    await page.wait_for_selector("hg-instant-league-round-picker li", state="attached", timeout=30000)
    await ensure_gng_selected(page)
    await wait_for_matches(page)

    s_rounds = "hg-instant-league-round-picker li"
    heures = await lire_heures_onglets(page)
    total = await page.locator(s_rounds).count()

    for i in range(total):
        onglet = page.locator(s_rounds).nth(i)
        brut = ((await onglet.text_content()) or "").strip()
        if not brut or "live" in brut.lower():
            continue
        h_round = heures[i]
        if not h_round:
            continue

        try:
            await onglet.scroll_into_view_if_needed()
        except Exception:
            pass
        await onglet.click(force=True)
        await page.wait_for_timeout(1500)
        await ensure_gng_selected(page)
        await wait_for_matches(page)

        for match in await extraire_matchs_round_courant(page):
            eq_dom_n = normaliser_nom_equipe(match["equipe_dom"])
            eq_ext_n = normaliser_nom_equipe(match["equipe_ext"])
            if eq_norm not in eq_dom_n and eq_norm not in eq_ext_n:
                continue
            if not (CONFIG["min_cote"] <= match["cote_oui"] <= CONFIG["max_cote"]):
                continue
            opportunite = {
                "round_index": i,
                "heure_round": h_round,
                "heure_brute": brut,
                **match,
            }
            cle = make_opportunity_key(opportunite)
            if cle in opportunites_traitees:
                continue
            opportunite["cle_opportunite"] = cle
            return opportunite
    return None


async def cliquer_afficher_plus_si_present(page, max_clicks=5):
    for _ in range(max_clicks):
        loc = page.locator("text=/Afficher plus/i").first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=4000)
                await page.wait_for_timeout(1200)
            else:
                break
        except Exception:
            break


async def extraire_lignes_resultats(page):
    conteneurs = page.locator("hg-instant-league-results .result-container")
    lignes = []
    total = await conteneurs.count()
    for i in range(total):
        cont = conteneurs.nth(i)
        try:
            header = (await cont.locator(".header").inner_text()).strip()
        except Exception:
            header = ""
        round_time = extraire_hhmm(header)
        rows = cont.locator(".match-results .row")
        for j in range(await rows.count()):
            row = rows.nth(j)
            try:
                teams = [x.strip() for x in await row.locator(".team span").all_inner_texts() if x.strip()]
                if len(teams) < 2:
                    continue
                score_text = (await row.locator(".match-score").inner_text()).strip()
                m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", score_text)
                if not m:
                    continue
                lignes.append({
                    "round_label": header,
                    "round_time": round_time,
                    "home_team": teams[0],
                    "away_team": teams[1],
                    "home_score": int(m.group(1)),
                    "away_score": int(m.group(2)),
                    "score": score_text,
                })
            except Exception:
                continue

    lignes.sort(key=lambda x: hhmm_vers_minutes(x.get("round_time")) or -1, reverse=True)
    return lignes


async def analyser_equipe_active(page, identifiant, mot_de_passe):
    await page.goto(URL_RESULTATS, wait_until="domcontentloaded", timeout=120000)
    await page.wait_for_timeout(2500)

    if await lire_bankroll(page) is None:
        await reinitialiser_et_se_connecter(page, URL_RESULTATS, identifiant, mot_de_passe)
        await page.goto(URL_RESULTATS, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(2500)

    await cliquer_afficher_plus_si_present(page, max_clicks=5)
    lignes = await extraire_lignes_resultats(page)
    if not lignes:
        return CONFIG["equipe_cible_defaut"], True, []

    compteur_oui = {}
    compteur_total = {}
    for ligne in lignes:
        home = normaliser_nom_equipe(ligne["home_team"])
        away = normaliser_nom_equipe(ligne["away_team"])
        is_oui = ligne["home_score"] >= 1 and ligne["away_score"] >= 1
        for eq in (home, away):
            if not eq:
                continue
            compteur_total[eq] = compteur_total.get(eq, 0) + 1
            if is_oui:
                compteur_oui[eq] = compteur_oui.get(eq, 0) + 1

    classement = []
    for eq, total in compteur_total.items():
        oui = compteur_oui.get(eq, 0)
        classement.append({
            "equipe": eq,
            "oui": oui,
            "total": total,
            "taux": round((oui / total) * 100, 2) if total else 0.0,
            "score": round(score_equipe(oui, total), 4),
        })

    classement.sort(key=lambda x: (x["score"], x["oui"], x["total"]), reverse=True)
    top5 = classement[:5]
    top5_noms = [x["equipe"] for x in top5]
    equipe_defaut = normaliser_nom_equipe(CONFIG["equipe_cible_defaut"])
    equipe_dans_top5 = equipe_defaut in top5_noms

    if equipe_dans_top5:
        equipe_active = equipe_defaut
    else:
        equipe_active = top5_noms[0] if top5_noms else equipe_defaut

    logger.info("TOP 5 calculé: %s", top5)
    return equipe_active, equipe_dans_top5, top5


async def verifier_resultat_gng(page, heure_mise, equipe_dom_cible, equipe_ext_cible, identifiant, mot_de_passe, essais_max=20):
    heure_cible = extraire_hhmm(heure_mise) or heure_mise
    dom_cible = normaliser_nom_equipe(equipe_dom_cible)
    ext_cible = normaliser_nom_equipe(equipe_ext_cible)

    for essai in range(1, essais_max + 1):
        await page.goto(URL_RESULTATS, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(2500)

        if await lire_bankroll(page) is None:
            await reinitialiser_et_se_connecter(page, URL_RESULTATS, identifiant, mot_de_passe)
            await page.goto(URL_RESULTATS, wait_until="domcontentloaded", timeout=120000)
            await page.wait_for_timeout(2500)

        await cliquer_afficher_plus_si_present(page, max_clicks=3)
        lignes = await extraire_lignes_resultats(page)

        if not lignes:
            logger.info("Résultats vides (scan %s/%s).", essai, essais_max)
            await page.wait_for_timeout(CONFIG["pause_resultats_sec"] * 1000)
            continue

        for ligne in lignes:
            heure_ok = (ligne["round_time"] == heure_cible) if heure_cible else True
            if (heure_ok and normaliser_nom_equipe(ligne["home_team"]) == dom_cible
                    and normaliser_nom_equipe(ligne["away_team"]) == ext_cible):
                return "OUI" if (ligne["home_score"] >= 1 and ligne["away_score"] >= 1) else "NON"

        dernier = lignes[0].get("round_time")
        if heure_cible and comparer_hhmm(dernier, heure_cible) == 1:
            return "PERTE_TIMEOUT"

        await page.wait_for_timeout(CONFIG["pause_resultats_sec"] * 1000)

    return "INDETERMINE"


def initialiser_etat_strategie(bankroll_initiale):
    bankroll_initiale = bankroll_initiale or 0.0
    return {
        "serie_id": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        "bankroll_initiale": bankroll_initiale,
        "bankroll_derniere_lecture": bankroll_initiale,
        "perte_cumulee": 0.0,
        "defaites_consecutives": 0,
        "marche_perdant_precedent": None,
        "historique": [],
        "rapport_20_genere": False,
        "resets_apres_pertes": 0,
        "equipe_active": CONFIG["equipe_cible_defaut"],
        "mb_dans_top5": True,
        "top5_dernier": [],
        "dernier_cycle_analyse": 0,
        "opportunites_traitees": set(),
    }


def calculer_plan_mises(etat, opportunite, bankroll_courante):
    mode = CONFIG["mode_pari"].strip().lower()
    base = CONFIG["mise_base"]
    perte = etat["perte_cumulee"]

    if mode == "double":
        mise_oui = base
        mise_non = base
        if perte > 0:
            perdant = etat.get("marche_perdant_precedent")
            if perdant == "oui":
                mise_oui = calculer_mise_recuperation(perte, opportunite["cote_oui"], bankroll_courante)
            elif perdant == "non":
                mise_non = calculer_mise_recuperation(perte, opportunite["cote_non"], bankroll_courante)
    elif mode == "non":
        mise_oui = 0.0
        mise_non = base if perte <= 0 else calculer_mise_recuperation(perte, opportunite["cote_non"], bankroll_courante)
    else:
        mise_oui = base if perte <= 0 else calculer_mise_recuperation(perte, opportunite["cote_oui"], bankroll_courante)
        mise_non = 0.0

    engagement = round(mise_oui + mise_non, 2)
    return {
        "mode": mode,
        "mise_oui": round(mise_oui, 2),
        "mise_non": round(mise_non, 2),
        "engagement_total": engagement,
    }


def decrire_mode_pari(mode):
    return {"double": "Oui + Non", "oui": "Oui uniquement", "non": "Non uniquement"}[mode]


def session_doit_s_arreter(etat, bankroll_courante):
    bk_init = etat["bankroll_initiale"] or 0.0
    if etat["defaites_consecutives"] >= CONFIG["max_defaites_session"]:
        return True, f"Arrêt sécurité: {etat['defaites_consecutives']} défaites consécutives."
    if bk_init > 0 and bankroll_courante is not None:
        perte_pct = ((bk_init - bankroll_courante) / bk_init) * 100.0
        if perte_pct >= CONFIG["stop_loss_pct"]:
            return True, f"Arrêt stop-loss: {perte_pct:.2f}% de perte sur la bankroll."
    return False, None


def ecrire_log_jsonl(output_dir, payload):
    path = output_dir / "journal_cycles.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def construire_rapport_20_rencontres(etat, output_dir):
    hist = etat["historique"][: CONFIG["taille_rapport"]]
    if len(hist) < CONFIG["taille_rapport"]:
        return None
    bk_i = etat["bankroll_initiale"]
    bk_f = etat["bankroll_derniere_lecture"]
    total_eng = round(sum(x["engagement_total"] for x in hist), 2)
    profit = round(sum(x["profit_net"] for x in hist), 2)
    gains = sum(1 for x in hist if x["resultat_net"] == "GAIN")
    pertes = sum(1 for x in hist if x["resultat_net"] == "PERTE")

    rapport = {
        "serie_id": etat["serie_id"],
        "genere_le": now_utc_iso(),
        "strategie": {
            "marche": "G/NG",
            "mode_pari": decrire_mode_pari(CONFIG["mode_pari"]),
            "mise_base": CONFIG["mise_base"],
            "stop_loss_pct": CONFIG["stop_loss_pct"],
            "max_engagement_pct": CONFIG["max_engagement_pct"],
        },
        "observations_20": {
            "gains_nets": gains,
            "pertes_nettes": pertes,
            "taux_reussite_pct": round(gains / len(hist) * 100, 2),
            "engagement_total": total_eng,
            "profit_net": profit,
            "bankroll_initiale": bk_i,
            "bankroll_finale": bk_f,
        },
        "historique": hist,
    }

    json_path = output_dir / f"rapport_congobet_v6_2_{etat['serie_id']}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rapport, f, ensure_ascii=False, indent=2)
    logger.info("Rapport enregistré: %s", json_path)
    return rapport


async def placer_un_pari(page, element_cote, mise):
    try:
        for purge_sel in ["#BetSlip_DeleteAll", "button:has-text('Supprimer tout')"]:
            try:
                loc = page.locator(purge_sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=1500)
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                pass

        await element_cote.scroll_into_view_if_needed()
        await element_cote.click(force=True)
        await page.wait_for_timeout(800)

        champ = page.locator("#NumberPicker_Value, input[type='number']").first
        if not await safe_fill(champ, str(int(mise) if float(mise).is_integer() else round(mise, 2))):
            return False, "saisie_mise_invalide"

        btn = page.locator("#BetSlip_PlaceMyBet, button:has-text('Place My Bet'), button:has-text('Parier')").first
        if not await safe_click(btn, timeout=8000):
            return False, "bouton_pari_introuvable"

        await page.wait_for_timeout(2500)
        page_text = clean_text(await page.text_content("body"))
        confirme = any(msg in page_text for msg in CONFIG["messages_confirmation_pari"])
        return confirme, ("ok" if confirme else "confirmation_non_detectee")
    except Exception as e:
        return False, str(e)


async def placer_paris_gng(page, opportunite, mise_oui, mise_non):
    paris = []
    if mise_oui and mise_oui > 0:
        paris.append(("oui", mise_oui, opportunite["el_oui"]))
    if mise_non and mise_non > 0:
        paris.append(("non", mise_non, opportunite["el_non"]))
    if not paris:
        return False, "aucun_pari"

    for sens, mise, element in paris:
        ok, statut = await placer_un_pari(page, element, mise)
        if not ok:
            return False, f"{sens}:{statut}"
    return True, "ok"


async def executer_cycle_continu_manchester_blue_gng(page, url_cible, identifiant, mot_de_passe):
    if await lire_bankroll(page) is None:
        ok = await reinitialiser_et_se_connecter(page, url_cible, identifiant, mot_de_passe)
        if not ok:
            raise RuntimeError("Impossible d'initialiser la session authentifiée.")

    bankroll_initiale = await lire_bankroll(page)
    etat = initialiser_etat_strategie(bankroll_initiale)
    logger.info("Bankroll initiale détectée: %s", bankroll_initiale)

    numero_cycle = 0
    while True:
        numero_cycle += 1
        if CONFIG["max_cycles"] and numero_cycle > CONFIG["max_cycles"]:
            logger.info("Limite de cycles atteinte.")
            break

        bankroll_avant = await lire_bankroll(page)
        if bankroll_avant is not None:
            etat["bankroll_derniere_lecture"] = bankroll_avant

        stop, raison = session_doit_s_arreter(etat, bankroll_avant)
        if stop:
            logger.warning("%s", raison)
            break

        doit_analyser = numero_cycle == 1 or (numero_cycle - etat["dernier_cycle_analyse"]) >= CONFIG["analyse_tous_les_n_cycles"]
        if doit_analyser:
            equipe_active, mb_dans_top5, top5 = await analyser_equipe_active(page, identifiant, mot_de_passe)
            etat["equipe_active"] = equipe_active
            etat["mb_dans_top5"] = mb_dans_top5
            etat["top5_dernier"] = top5
            etat["dernier_cycle_analyse"] = numero_cycle
            await page.goto(url_cible, wait_until="domcontentloaded", timeout=120000)
            await page.wait_for_timeout(2000)

        equipe_active = etat["equipe_active"]
        logger.info(
            "Cycle #%s | équipe active: %s | perte cumulée: %s FR",
            numero_cycle,
            equipe_active.title(),
            etat["perte_cumulee"],
        )

        opportunite = await trouver_match_du_cycle(page, url_cible, equipe_active, etat["opportunites_traitees"])
        if not opportunite:
            logger.info("Aucune opportunité nouvelle trouvée.")
            await page.wait_for_timeout(CONFIG["pause_recherche_sec"] * 1000)
            continue

        plan = calculer_plan_mises(etat, opportunite, bankroll_avant)
        if bankroll_avant is not None and plan["engagement_total"] > bankroll_avant:
            logger.warning("Solde insuffisant pour l'engagement calculé. Cycle ignoré.")
            await page.wait_for_timeout(CONFIG["pause_recherche_sec"] * 1000)
            continue

        logger.info(
            "Opportunité: %s vs %s | heure=%s | OUI=%s NON=%s",
            opportunite["equipe_dom"],
            opportunite["equipe_ext"],
            opportunite["heure_round"],
            opportunite["cote_oui"],
            opportunite["cote_non"],
        )
        logger.info(
            "Plan de mises: OUI=%s FR | NON=%s FR | engagement=%s FR",
            plan["mise_oui"],
            plan["mise_non"],
            plan["engagement_total"],
        )

        ok, statut_pari = await placer_paris_gng(page, opportunite, plan["mise_oui"], plan["mise_non"])
        if not ok:
            logger.warning("Pari non confirmé: %s", statut_pari)
            await page.wait_for_timeout(CONFIG["pause_recherche_sec"] * 1000)
            continue

        etat["opportunites_traitees"].add(opportunite["cle_opportunite"])
        await page.wait_for_timeout(CONFIG["pause_apres_pari_sec"] * 1000)

        gng = await verifier_resultat_gng(
            page,
            opportunite["heure_round"],
            opportunite["equipe_dom"],
            opportunite["equipe_ext"],
            identifiant,
            mot_de_passe,
        )

        bankroll_apres = await lire_bankroll(page)
        if bankroll_apres is not None:
            etat["bankroll_derniere_lecture"] = bankroll_apres

        if gng == "OUI":
            gain = round(plan["mise_oui"] * opportunite["cote_oui"], 2)
            profit_net = round(gain - plan["engagement_total"], 2)
            perte_nette = max(0.0, -profit_net)
            marche_perd = "non"
        elif gng == "NON":
            gain = round(plan["mise_non"] * opportunite["cote_non"], 2)
            profit_net = round(gain - plan["engagement_total"], 2)
            perte_nette = max(0.0, -profit_net)
            marche_perd = "oui"
        elif gng == "PERTE_TIMEOUT":
            gain = 0.0
            profit_net = round(-plan["engagement_total"], 2)
            perte_nette = plan["engagement_total"]
            marche_perd = etat.get("marche_perdant_precedent") or "oui"
        else:
            gain = 0.0
            profit_net = 0.0
            perte_nette = 0.0
            marche_perd = None

        resultat_net = "GAIN" if profit_net > 0 else ("PERTE" if profit_net < 0 else "INDETERMINE")

        reset_applique = False
        if resultat_net == "PERTE":
            etat["defaites_consecutives"] += 1
            etat["perte_cumulee"] = round(etat["perte_cumulee"] + perte_nette, 2)
            etat["marche_perdant_precedent"] = marche_perd
            if etat["defaites_consecutives"] >= CONFIG["reset_apres_defaites"]:
                etat["defaites_consecutives"] = 0
                etat["perte_cumulee"] = 0.0
                etat["marche_perdant_precedent"] = None
                etat["resets_apres_pertes"] += 1
                reset_applique = True
        elif resultat_net == "GAIN":
            etat["defaites_consecutives"] = 0
            etat["perte_cumulee"] = 0.0
            etat["marche_perdant_precedent"] = None

        item = {
            "numero": len(etat["historique"]) + 1,
            "horodatage_utc": now_utc_iso(),
            "equipe_active": equipe_active,
            "mb_dans_top5": etat["mb_dans_top5"],
            "round_index": opportunite["round_index"],
            "heure_round": opportunite["heure_round"],
            "rencontre": f"{opportunite['equipe_dom']} vs {opportunite['equipe_ext']}",
            "cote_oui": opportunite["cote_oui"],
            "cote_non": opportunite["cote_non"],
            "mise_oui": plan["mise_oui"],
            "mise_non": plan["mise_non"],
            "engagement_total": plan["engagement_total"],
            "gng": gng,
            "gain": gain,
            "profit_net": profit_net,
            "resultat_net": resultat_net,
            "bankroll_avant": bankroll_avant,
            "bankroll_apres": bankroll_apres,
            "reset_apres_n_pertes": reset_applique,
        }
        if resultat_net in {"GAIN", "PERTE"}:
            etat["historique"].append(item)
        ecrire_log_jsonl(OUTPUT_DIR, item)

        logger.info("Résultat: %s | profit net=%+.2f FR | bankroll=%s", gng, profit_net, bankroll_apres)
        if reset_applique:
            logger.warning("Reset sécurité appliqué après série de pertes.")

        if len(etat["historique"]) >= CONFIG["taille_rapport"] and not etat["rapport_20_genere"]:
            construire_rapport_20_rencontres(etat, OUTPUT_DIR)
            etat["rapport_20_genere"] = True

        await page.goto(url_cible, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(CONFIG["pause_apres_pari_sec"] * 1000)

    return etat


async def main():
    identifiant, mot_de_passe = lire_identifiants()
    pw = browser = context = page = None
    try:
        pw, browser, context, page = await initialiser_navigateur(URL_INSTANT_LEAGUE)
        await executer_cycle_continu_manchester_blue_gng(
            page,
            URL_INSTANT_LEAGUE,
            identifiant,
            mot_de_passe,
        )
    finally:
        await fermer_session(pw, browser, context)
        logger.info("Session fermée proprement.")


if __name__ == "__main__":
    asyncio.run(main())

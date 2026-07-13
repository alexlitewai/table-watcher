#!/usr/bin/env python3
"""
Bot de réservation de table via l'API Zenchef.

Fonctionnement :
 1. Interroge les disponibilités des dates cibles (fenêtre décembre puis janvier).
 2. Dès qu'un créneau dîner pour `pax` personnes est ouvert sur une date cible,
    tente la réservation via POST /booking (avec le couple timestamp/auth-token
    fourni par GET /getAuthToken).
 3. Si la réservation nécessite une empreinte bancaire ou échoue, notifie
    immédiatement (issue GitHub) avec le lien du widget pour finaliser à la main.
 4. L'état est persisté dans state.json pour ne jamais réserver deux fois.

Aucune dépendance externe : stdlib uniquement.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://bookings-middleware.zenchef.com"
WIDGET_URL = "https://bookings.zenchef.com/results?rid={rid}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")

# Heure de Paris sans dépendance : UTC+1 (hiver) / UTC+2 (été).
# Les dates cibles sont en décembre/janvier → UTC+1. Approximation correcte ici.
PARIS_OFFSET_WINTER = 1


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def http_json(method, path, payload=None, headers=None, retries=3, backoff=50):
    """Appel HTTP JSON avec retries espacés (l'API rate-limite ~1 req/min en rafale)."""
    url = BASE + path
    base_headers = {
        "Accept": "application/json",
        "Origin": "https://bookings.zenchef.com",
        "Referer": "https://bookings.zenchef.com/",
        "User-Agent": UA,
    }
    if headers:
        base_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        base_headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=base_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                last_err = f"HTTP {e.code}: {body[:300]}"
        except Exception as e:  # réseau, timeout, JSON invalide (page WAF…)
            last_err = str(e)
        if attempt < retries:
            log(f"  {method} {path.split('?')[0]} tentative {attempt} échouée ({last_err}) — attente {backoff}s")
            time.sleep(backoff)
    return None, {"error": {"message": f"unreachable: {last_err}"}}


PII_KEYS = {"firstname", "lastname", "email", "phone", "phone_number",
            "customersheet", "customer", "civility", "optins", "country"}


def sanitize(obj):
    """Masque les champs personnels d'une réponse API avant log/notification."""
    if isinstance(obj, dict):
        return {k: ("<masqué>" if k in PII_KEYS else sanitize(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


def get_auth():
    status, data = http_json("GET", f"/getAuthToken?restaurantId={RID}")
    if status == 200 and "authToken" in data:
        return data["timestamp"], data["authToken"]
    log(f"Impossible d'obtenir l'auth token: {data}")
    return None, None


def is_dinner_shift(shift):
    name = (shift.get("name") or "").lower()
    if any(k in name for k in ("dîner", "diner", "dinner", "soir")):
        return True
    return (shift.get("open") or "") >= "17:00"


def paris_now():
    return datetime.now(timezone.utc) + timedelta(hours=PARIS_OFFSET_WINTER)


def slot_window_ok(obj):
    """Respecte bookable_from / bookable_to (format 'YYYY-MM-DD HH:MM:SS', heure de Paris)."""
    now = paris_now().strftime("%Y-%m-%d %H:%M:%S")
    bf, bt = obj.get("bookable_from"), obj.get("bookable_to")
    if bf and now < bf:
        return False
    if bt and now > bt:
        return False
    return True


def find_bookable_slots(day_data, pax, meal="dinner"):
    """Retourne [(time, shift), ...] triés, réservables pour `pax` (dîner par défaut)."""
    out = []
    for shift in day_data.get("shifts", []):
        if meal != "any" and not is_dinner_shift(shift):
            continue
        if shift.get("marked_as_full"):
            continue
        if not slot_window_ok(shift):
            continue
        for slot in shift.get("shift_slots", []):
            if slot.get("closed") or slot.get("marked_as_full"):
                continue
            if not slot_window_ok(slot):
                continue
            if pax in (slot.get("possible_guests") or []):
                out.append((slot.get("slot_name") or slot.get("name"), shift))
    return sorted(out, key=lambda x: x[0])


def pick_slot(slots, preferred="20:00"):
    """Choisit le créneau le plus proche de l'heure préférée."""
    def minutes(t):
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    pref = minutes(preferred)
    return min(slots, key=lambda x: abs(minutes(x[0]) - pref))


def make_booking(cfg, day, slot_time):
    ts, token = get_auth()
    if not token:
        return False, {"error": "no auth token"}
    c = cfg["customer"]
    payload = {
        "day": day,
        "nb_guests": cfg["pax"],
        "time": slot_time,
        "lang": c.get("lang", "fr"),
        "firstname": c["firstname"].strip(),
        "lastname": c["lastname"].strip(),
        "civility": c["civility"],
        "country": c["country"],
        "phone_number": c["phone_number"].replace(" ", ""),
        "email": c["email"].strip(),
        "comment": c.get("comment", ""),
        "custom_field": {},
        "custom_field_v2": [],
        "customersheet": {
            "firstname": c["firstname"].strip(),
            "lastname": c["lastname"].strip(),
            "civility": c["civility"],
            "phone": c["phone_number"].replace(" ", ""),
            "email": c["email"].strip(),
            "optins": [{"type": "review_mail", "value": True}],
            "country": c["country"],
            "lang": c.get("lang", "fr"),
        },
        "offers": [],
        "partner_id": "1001",
        "type": "web",
    }
    status, resp = http_json(
        "POST", f"/booking?restaurantId={RID}", payload,
        headers={"timestamp": str(ts), "auth-token": token},
        retries=2, backoff=65,
    )
    log(f"POST /booking → HTTP {status}: {json.dumps(sanitize(resp), ensure_ascii=False)[:500]}")
    ok = status in (200, 201) and isinstance(resp, dict) and ("id" in resp or "uuid" in resp)
    return ok, resp


def notify_once(state, key, title, body, ttl_hours=12):
    """Comme notify(), mais au plus une fois par `key` toutes les ttl_hours."""
    now = datetime.now(timezone.utc)
    sent = state.setdefault("notified", {})
    last = sent.get(key)
    if last and datetime.fromisoformat(last) + timedelta(hours=ttl_hours) > now:
        log(f"Notification '{key}' déjà envoyée récemment — silencieux.")
        return
    sent[key] = now.isoformat()
    save_state(state)
    notify(title, body)


def notify(title, body):
    """Crée une issue GitHub (dans Actions) → notification e-mail native GitHub."""
    log(f"NOTIFICATION: {title}\n{body}")
    tok = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body}).encode(),
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "table-watcher",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"Issue créée: HTTP {r.status}")
    except Exception as e:
        log(f"Échec création issue: {e}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## {title}\n\n{body}\n")


def group_targets(targets):
    """Groupe les dates cibles contiguës/proches pour minimiser les requêtes."""
    groups = []
    for d in sorted(targets):
        if groups and (datetime.fromisoformat(d) - datetime.fromisoformat(groups[-1][-1])).days <= 7:
            groups[-1].append(d)
        else:
            groups.append([d])
    return groups


def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        log("config.json introuvable"); sys.exit(1)
    # Les secrets/PII peuvent venir de l'environnement (GitHub Secrets).
    c = cfg["customer"]
    for key, env in [("firstname", "RESA_FIRSTNAME"), ("lastname", "RESA_LASTNAME"),
                     ("email", "RESA_EMAIL"), ("phone_number", "RESA_PHONE"),
                     ("civility", "RESA_CIVILITY")]:
        if os.environ.get(env):
            c[key] = os.environ[env]

    global RID
    RID = cfg["restaurant_id"]
    pax = cfg["pax"]
    targets = cfg["targets"]  # ordre = priorité
    state = load_json(STATE_PATH, {"status": "watching", "seen": {}})

    today = paris_now().strftime("%Y-%m-%d")
    if state.get("last_heartbeat") != today:
        state["last_heartbeat"] = today
        save_state(state)

    if state.get("status") == "booked":
        log(f"Déjà réservé ({state.get('booking', {})}) — rien à faire.")
        return
    if state.get("status") == "manual_action_required":
        log("Action manuelle requise (voir notifications) — le bot n'insiste pas. "
            "Remettre \"status\": \"watching\" dans state.json pour relancer.")
        return

    missing = [k for k in ("firstname", "lastname", "email", "phone_number", "civility") if not c.get(k)]
    if missing and cfg.get("mode", "book") == "book":
        log(f"ATTENTION: champs client manquants {missing} — mode notification seulement.")

    # Interroge chaque groupe de dates (une requête par groupe, espacées).
    availabilities = {}
    groups = group_targets(targets)
    for i, group in enumerate(groups):
        if i > 0:
            time.sleep(cfg.get("request_spacing_seconds", 45))
        begin, end = group[0], group[-1]
        status, data = http_json("GET", f"/getAvailabilities?restaurantId={RID}&date_begin={begin}&date_end={end}")
        if status != 200 or not isinstance(data, list):
            log(f"Échec getAvailabilities {begin}..{end}: HTTP {status} {str(data)[:200]}")
            continue
        for day in data:
            if day.get("date") in targets:
                availabilities[day["date"]] = day

    if not availabilities:
        notify_once(state, "api-down", "⚠️ API injoignable",
                    "Aucune réponse exploitable de l'API Zenchef sur ce run "
                    "(rate limit, WAF/captcha ou panne). Si cela persiste sur "
                    "plusieurs runs consécutifs, vérifier les logs du workflow.",
                    ttl_hours=24)
        sys.exit(1)

    # Résumé de l'état des dates cibles (dans l'ordre de priorité).
    candidates = []
    for d in targets:
        day = availabilities.get(d)
        if not day:
            log(f"{d}: pas de données")
            continue
        slots = find_bookable_slots(day, pax, cfg.get("meal", "dinner"))
        n_shifts = len(day.get("shifts", []))
        log(f"{d}: {n_shifts} service(s), créneaux dîner x{pax}: {[s[0] for s in slots] or 'aucun'}")
        if slots:
            candidates.append((d, slots))

    if not candidates:
        log("Aucune date cible ouverte pour l'instant. Fin du run.")
        return

    # Une date est ouverte !
    day, slots = candidates[0]
    slot_time, shift = pick_slot(slots, cfg.get("preferred_time", "20:00"))
    widget = WIDGET_URL.format(rid=RID)
    charge = shift.get("charge_param") or {}
    needs_imprint = bool(charge.get("is_web_booking_askable"))
    others = "\n".join(f"- {d} : {', '.join(s[0] for s in sl)}" for d, sl in candidates)

    if cfg.get("mode", "book") != "book" or missing:
        notify_once(state, f"open:{day}",
                    f"🔔 OUVERT le {day} — réserver vite !",
                    f"Créneaux dîner {pax} pers. disponibles :\n{others}\n\n"
                    f"Réserver ici : {widget}\n"
                    f"(Créneau conseillé : {day} à {slot_time})")
        return

    ok, resp = make_booking(cfg, day, slot_time)
    if ok:
        state["status"] = "booked"
        state["booking"] = {
            "date": day, "time": slot_time, "pax": pax,
            "id": resp.get("id"), "uuid": resp.get("uuid"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state)
        confirmation_note = "Le restaurant confirme manuellement : surveiller l'e-mail de confirmation Zenchef."
        if needs_imprint:
            confirmation_note += ("\n⚠️ Une empreinte bancaire peut être demandée pour finaliser — "
                                  f"vérifiez l'e-mail Zenchef ou le widget : {widget}")
        notify(f"✅ Réservé : {day} à {slot_time} ({pax} pers.)",
               f"Réservation créée (id: {resp.get('id')}, uuid: {resp.get('uuid')}).\n"
               f"{confirmation_note}")
    else:
        state["status"] = "manual_action_required"
        save_state(state)
        err = json.dumps(sanitize(resp), ensure_ascii=False)[:800]
        notify_once(state, f"bookfail:{day}", ttl_hours=6,
                    title=f"🔔 OUVERT le {day} — réservation auto ÉCHOUÉE, agir vite !",
                    body=f"Créneaux disponibles :\n{others}\n\n"
                    f"La réservation automatique a échoué : `{err}`\n"
                    f"{'⚠️ Empreinte bancaire requise par le restaurant — à faire à la main. ' if needs_imprint else ''}"
                    f"Réservez manuellement ici : {widget}")
        sys.exit(1)


if __name__ == "__main__":
    main()

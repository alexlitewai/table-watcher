# table-watcher

Surveille les disponibilités d'un restaurant utilisant Zenchef et réserve
automatiquement une table dès qu'une des dates cibles ouvre à la réservation
en ligne. Les dates, l'heure préférée et le nombre de couverts sont dans
`config.json` (`targets` est ordonné par priorité). Les coordonnées du client
proviennent exclusivement de variables d'environnement / secrets GitHub :
`RESA_CIVILITY`, `RESA_FIRSTNAME`, `RESA_LASTNAME`, `RESA_EMAIL`, `RESA_PHONE`.

## Fonctionnement

- `bot.py` (Python 3, stdlib uniquement) interroge l'API du widget Zenchef
  (`bookings-middleware.zenchef.com`) :
  1. `getAvailabilities` sur les fenêtres de dates cibles (requêtes espacées,
     l'API limite les rafales à ~1 req/min par IP) ;
  2. un créneau est réservable si `possible_guests` contient le nombre de
     couverts sur un slot du service du soir ;
  3. réservation via `getAuthToken` + `POST /booking` (payload identique au
     widget officiel) ;
  4. notification par issue GitHub (e-mail automatique) en cas de succès,
     d'échec ou d'action manuelle requise — avec dédoublonnage.
- `state.json` mémorise la réservation effectuée → jamais de double réservation ;
  une fois réservé, les runs suivants s'arrêtent immédiatement.
- Le workflow GitHub Actions tourne toutes les 5 minutes dans le cloud.

## Points d'attention (relevés sur l'API en juillet 2026)

- **Horizon d'ouverture** : ~110 jours glissants. Les dates cibles s'ouvrent
  environ 3 mois et demi à l'avance ; le bot attend leur ouverture.
- **Empreinte bancaire** : le service du soir peut exiger une empreinte CB
  (`charge_param.is_web_booking_askable`). Si le POST est refusé pour cette
  raison, le bot notifie immédiatement avec le lien du widget pour finaliser
  à la main (aucune donnée bancaire n'est automatisée).
- **Confirmation manuelle** : le restaurant valide chaque réservation à la
  main (`confirmation.is_auto = false`) — surveiller l'e-mail Zenchef.
- **WAF AWS** : un captcha peut apparaître sous forte suspicion ; le bot le
  détecte (réponse non-JSON ou `bm.invalid_token`) et bascule en notification.

## Test local

```bash
python3 bot.py
```

Sans les variables `RESA_*`, le bot fonctionne en lecture seule (notification
uniquement, aucune réservation).

## Arrêter

Désactiver le workflow dans l'onglet Actions, ou supprimer le dépôt.

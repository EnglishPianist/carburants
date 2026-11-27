# Carburants

Les prix des carburants autour de toi, avec ce que les sites habituels ne montrent pas :
- si le prix d'une station est **bas ou haut pour elle** par rapport à ses 90 derniers jours ;
- la **variation sur 7 jours** ;
- le **jour de la semaine** où chaque station (et chaque zone) est en général la moins chère ;
- l'historique sur 90 jours comparé à la médiane du département.

Page : https://englishpianist.github.io/carburants/ (Paris par défaut ; « Changer » pour une adresse ou ta position).

## Fonctionnement
- `.github/workflows/build.yml` tourne toutes les 30 minutes :
  - prix actuels : flux instantané officiel (`donnees.roulez-eco.fr/opendata/instantane`) ;
  - une fois par jour : historique officiel de l'année (`donnees.roulez-eco.fr/opendata/annee/AAAA`) → statistiques, gardées dans le cache Actions ;
  - publication sur GitHub Pages (rien n'est commité, le dépôt reste léger).
- `scripts/build.py` : téléchargement, calculs, tuiles JSON de 0,25°.
- `site/index.html` : la page (aucune dépendance).

Source : prix-carburants.gouv.fr (Licence Ouverte). Les données ne contiennent pas le nom de l'enseigne.

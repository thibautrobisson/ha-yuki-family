# Yuki Family pour Home Assistant

Relie ton Home Assistant à l'appli [Yuki Family](https://yuki-family.fr) **sans l'ouvrir sur Internet** : ta maison appelle Yuki, jamais l'inverse. Pas d'adresse publique, pas de jeton à copier.

## Installation (HACS)

1. Dans Home Assistant, installe [HACS](https://hacs.xyz/docs/use/) si ce n'est pas déjà fait.
2. HACS → ⋮ en haut à droite → **Dépôts personnalisés** : colle `https://github.com/thibautrobisson/ha-yuki-family`, catégorie **Intégration**, puis **Ajouter**.
3. Cherche **Yuki Family** dans HACS → **Télécharger**, puis redémarre Home Assistant.

## Relier ta maison

1. Dans l'appli Yuki Family : **Domotique Maison** → « Oui, j'ai Home Assistant » → **Obtenir mon code**.
2. Dans Home Assistant : **Paramètres → Appareils et services → Ajouter une intégration → Yuki Family**, colle le code (valable 10 minutes).
3. C'est tout : l'appli affiche ta maison, même en 4G.

## Ce que fait l'intégration

- Elle crée un utilisateur système « Yuki Family » (administrateur, pour allumer tes appareils et ajouter des marques depuis l'appli). Il est supprimé avec l'intégration.
- Elle garde une connexion sortante (HTTPS) avec le serveur de Yuki Family et rejoue sur ton Home Assistant les demandes de l'appli (lire les appareils, les allumer, la météo, les images des caméras…).
- Le jeton de Home Assistant ne quitte jamais ta maison. Le serveur ne connaît qu'un secret propre à la maison.
- Déconnectée dans l'appli, l'intégration demande un nouveau code (réparation dans Home Assistant).

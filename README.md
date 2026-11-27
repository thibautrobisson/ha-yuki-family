# Yuki Family pour Home Assistant

Relie ton Home Assistant à l'appli [Yuki Family](https://yuki-family.fr) **sans l'ouvrir sur Internet** : ta maison appelle Yuki, jamais l'inverse. Pas d'adresse publique, pas de jeton à copier.

## Installation (HACS)

1. Dans Home Assistant, installe [HACS](https://hacs.xyz/docs/use/) si ce n'est pas déjà fait.
2. Dans HACS, cherche **Yuki Family** → **Télécharger**, puis redémarre Home Assistant.

Si Yuki Family n'apparaît pas encore dans HACS : HACS → ⋮ en haut à droite → **Dépôts personnalisés**, colle `https://github.com/thibautrobisson/ha-yuki-family`, catégorie **Intégration**, **Ajouter**, puis reprends l'étape 2.

## Relier ta maison

1. Dans l'appli Yuki Family : **Domotique Maison** → « Oui, j'ai Home Assistant » → **Obtenir mon code**.
2. Dans Home Assistant : **Paramètres → Appareils et services → Ajouter une intégration → Yuki Family**, colle le code (valable 10 minutes).
3. C'est tout : l'appli affiche ta maison, même en 4G.

## Ce que fait l'intégration

- Elle garde une connexion sortante avec le serveur de Yuki Family, en HTTPS dont le certificat est toujours vérifié, et y prend les demandes de l'appli (lire tes appareils, les allumer, la météo, les images des caméras…).
- Elle ne fait que les demandes de la **liste autorisée** ci-dessous et refuse tout le reste (une ligne « Demande de Yuki Family refusée » apparaît alors dans le journal de Home Assistant).
- Elle crée un utilisateur système « Yuki Family », **simple utilisateur (pas administrateur)**, supprimé avec l'intégration. Les versions 1.0.x le créaient administrateur : la mise à jour le rétrograde toute seule.
- Le jeton de Home Assistant ne quitte jamais ta maison. Le serveur ne connaît qu'un secret propre à la maison.
- Déconnectée dans l'appli, l'intégration demande un nouveau code (réparation dans Home Assistant).

## Liste autorisée

La même liste est dans le code : [`allowlist.py`](custom_components/yuki_family/allowlist.py).

**Lectures et services**, rejoués sur l'API locale de Home Assistant (127.0.0.1) avec l'utilisateur « Yuki Family » : Home Assistant y applique ses propres droits, donc un service réservé aux administrateurs (redémarrer, supprimer…) est refusé.

| Demande | Sert à |
| --- | --- |
| WebSocket `get_states` | lire l'état des appareils |
| WebSocket `config/entity_registry/list`, `config/device_registry/list`, `config/area_registry/list` | lister les appareils, leurs entités et les pièces |
| WebSocket `recorder/statistics_during_period` | la consommation par jour, semaine, mois |
| WebSocket `search/related` | les automatisations liées à un appareil |
| WebSocket `manifest/list` | le nom et le type des marques |
| WebSocket `call_service` | allumer, éteindre, régler un appareil, la météo du jour |
| REST `GET /api/camera_proxy/camera.<nom>` | l'image d'une caméra |

**Réglages réservés aux administrateurs**, faits par l'intégration elle-même, un par un, avec ces seuls paramètres :

| Demande | Paramètres acceptés | Sert à |
| --- | --- | --- |
| REST `GET /api/config/config_entries/flow_handlers?type=integration` | aucun | la liste des marques qu'on peut ajouter : celle de Home Assistant, plus tes intégrations personnalisées installées (HACS…) de type appareil, hub ou service, jamais Yuki Family |
| REST `POST /api/config/config_entries/flow` | `handler` : une marque de cette liste | commencer l'ajout d'une marque |
| REST `POST` et `DELETE /api/config/config_entries/flow/<id>` | seulement un ajout de marque ouvert par Yuki | répondre au formulaire de la marque, ou l'abandonner |
| WebSocket `config/device_registry/update` | `device_id` d'un appareil **d'une intégration ajoutée depuis Yuki**, `name_by_user`, `area_id` | renommer un appareil, le ranger dans une pièce |
| WebSocket `config/area_registry/create` | `name` | créer une pièce |
| WebSocket `config/entity_registry/update` | `entity_id` d'un `device_tracker` **désactivé par son intégration** (jamais par toi), `disabled_by` vide | réactiver un téléphone pour la présence à la maison |
| WebSocket `call_service` `homeassistant.reload_config_entry` | `entry_id` d'une **intégration ajoutée depuis Yuki** | « Actualiser » : recharger une intégration |
| WebSocket `automation/config` | `entity_id` d'une automatisation | lire **sa description seulement** (le programme d'un appareil) ; déclencheurs, conditions et actions ne sortent jamais |

**In English:** the relay only runs the requests listed above. Reads and service calls go through the local API as the non-admin "Yuki Family" system user, so Home Assistant's own permissions apply. The few admin-only calls the app needs (listing the brands that can be added — Home Assistant's own plus installed custom integrations of type device, hub or service —, adding a brand through its config flow, renaming a device or moving it to an area, creating an area, re-enabling a disabled `device_tracker`, reloading a config entry, reading an automation's config) are executed in-process by the integration, one by one, with only the parameters listed. Renaming/moving devices and reloading config entries only apply to config entries created by Yuki's own brand flows (their entry ids are stored when the flow finishes); a `device_tracker` is only re-enabled when its integration disabled it, never when you did; for an automation only its `description` leaves Home Assistant. Everything else is refused and logged.

Ajoutées autrement (directement dans Home Assistant, ou avant la version 1.2.0), tes intégrations et leurs appareils se renomment, se rangent et se rechargent dans Home Assistant.

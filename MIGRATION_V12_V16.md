# Migration Odoo v12 vers v16

Notes de travail pour migrer LMC_Creation_PO_et_Prix_FL vers Odoo v16 en
conservant la compatibilite v12. Le projet reste autonome et ne depend pas du
projet invoice_price_checker.

## Principe

Ne pas conditionner le code a une version Odoo explicite. Le code doit detecter
les capacites disponibles dans la base connectee, par exemple le champ
`partner_id` ou `name` sur `product.supplierinfo`.

## Etat observe le 2026-07-30

- La facture `data_files/FC260743575.pdf` fonctionne avec la base v12 locale
  `data_files/var_articles.data`.
- Le chargement direct d'une base v16 echoue sur `ir.model.data`, car
  l'utilisateur Odoo n'a pas acces au modele technique "Modele de Donnees".
- Le workflow local v12 de reference donne 24 lignes facture, 17 lignes
  trouvees, 6 non retrouvees, 1 ambigue, 14 changements de prix et 17 lignes de
  bon de commande.
- Le classeur de controle peut afficher, en mode developpeur, un onglet
  `bon_commande_id_numerique` pour comparer ID externe et ID numerique.

## Flag developpeur

Les outils de diagnostic doivent rester caches par defaut.

Activer le mode developpeur avec l'une des options suivantes :

- variable d'environnement `LMC_DEVELOPER_MODE=1`;
- ou section Streamlit :

```toml
[app]
developer_mode = true
```

## Etapes sequentielles proposees

1. Stabiliser la partie changement de prix Odoo.
   - Ajouter `timeout` a la connexion Odoo.
   - Detecter `product.supplierinfo.partner_id` ou `name`.
   - Lire les produits par `read` pour produire des erreurs explicites.
   - Ajouter un dry-run avant ecriture.
   - Ajouter un rapport detaille avant/apres.
   - Couvrir par tests unitaires v12/v16 avec faux modeles Odoo.

2. Adapter la lecture de la base articles.
   - Remplacer les `search_read` globaux par `search` puis `read` par lots.
   - Ajouter `batch_size` et `timeout` configurables.
   - Supprimer la dependance obligatoire a `ir.model.data`.
   - Conserver les colonnes LMC existantes utiles au workflow v12.
   - Detecter `partner_id` ou `name` sur `product.supplierinfo`.
   - Recuperer `product.margin.classification.markup` quand disponible.
   - Garder la compatibilite avec les anciennes bases locales v12 qui ont encore
     `ID Externe` et `Fournisseurs/ID Externe`.

3. Tester le chargement v16 depuis Streamlit.
   - Verifier que `data_files/var_articles.data` est regenere sans acces a
     `ir.model.data`.
   - Comparer les colonnes normalisees et le nombre de lignes avec la base v12.
   - Lancer la facture de reference en v16 sans ecriture Odoo.

4. Revenir ensuite a la partie bon de commande.
   - Adapter la creation directe API pour utiliser les IDs numeriques.
   - Ajouter une prevalidation/dry-run de creation PO.
   - Garder le CSV d'import manuel a part tant que son format exact n'est pas
     confirme.

5. Nettoyer les diagnostics temporaires.
   - Conserver seulement les outils utiles derriere le flag developpeur.
   - Retirer ou documenter les onglets temporaires si la migration PO est validee.

## Journal des modifications

### 2026-08-01 - Etape 1, changement de prix Odoo

Modifications locales realisees :

- ajout de `timeout` et `batch_size` dans `OdooConfig`, `config_from_env` et
  `config_from_mapping`;
- ajout du helper `supplierinfo_partner_field`, qui choisit `partner_id` quand
  disponible et retombe sur `name` pour Odoo v12;
- adaptation de la mise a jour prix pour utiliser ce champ detecte au lieu de
  coder `name` en dur;
- remplacement de `Product.browse` par `Product.read` dans l'ecriture prix pour
  produire des erreurs explicites quand l'article ou son template est absent;
- ajout d'un dry-run `dry_run_odoo_price_updates` qui lit Odoo sans ecrire et
  affiche les valeurs actuelles/futures;
- ajout dans Streamlit d'un bouton "Verifier la MaJ sans ecrire dans Odoo"; la
  mise a jour prix reelle reste bloquee tant que cette verification n'a pas ete
  faite sans erreur;
- ajout de l'onglet `dry_run_prix_odoo` dans le classeur de controle lorsqu'un
  dry-run prix a ete lance, pour conserver le rapport Odoo avant ecriture;
- deplacement du bouton de telechargement du classeur en bas de page Streamlit
  afin que les rapports produits pendant le flux, dont le dry-run prix, soient
  inclus dans l'export final;
- clarification du choix de base articles dans Streamlit avec trois options :
  utiliser la base disponible, choisir une autre base locale, ou telecharger la
  base depuis Odoo; l'upload de fichier n'apparait que pour l'option dediee;
- ajout de tests unitaires pour `partner_id` v16, `name` v12, la configuration
  `timeout`/`batch_size`, et le dry-run prix avec faux modeles Odoo.

Limite volontaire :

- la lecture de la base articles n'a pas encore ete modifiee dans cette etape;
  elle depend encore de `ir.model.data` et reste l'etape suivante.

### 2026-08-01 - Etape 2, lecture de la base articles

Modifications locales realisees :

- suppression de la lecture obligatoire de `ir.model.data` dans
  `fetch_articles_from_odoo`;
- extraction des articles, fournisseurs, unites, taxes et categories par
  `search` puis `read` par lots avec `batch_size`;
- connexion Odoo de l'extraction avec `timeout`;
- reutilisation de `supplierinfo_partner_field` pour lire le fournisseur de
  `product.supplierinfo` via `partner_id` en v16 ou `name` en v12;
- remplacement des colonnes generees `ID Externe` et `Fournisseurs/ID Externe`
  par des donnees non techniques dans les nouvelles bases extraites;
- conservation de la compatibilite avec les anciennes bases locales v12 qui
  contiennent encore `ID Externe` et `Fournisseurs/ID Externe`;
- recuperation de `product.margin.classification.markup` quand le modele et le
  champ existent;
- normalisation de `Categorie de marge/Markup` en `categorie_marge_markup`;
- calcul du prix de vente avec le markup Odoo en priorite, avec fallback sur le
  dictionnaire historique des categories de marge;
- ajout de tests unitaires pour la lecture par lots, la detection du champ
  fournisseur, la recuperation du markup et le fallback prix historique.

Limite volontaire :

- la creation de bon de commande n'est pas encore adaptee aux nouvelles bases
  sans `ID Externe`; elle reste prevue dans l'etape PO separee.

Validation utilisateur :

- la base articles v16 se charge depuis Streamlit sans l'erreur
  `ir.model.data`;
- le classeur `FC260743575_controle_odoo-7.xlsx` confirme que les nouvelles
  bases v16 n'ont plus de `db_id_externe`;
- le dry-run prix v16 passe avec 7 ligne(s) `ready`, 0 avertissement et
  0 erreur;
- le champ fournisseur detecte dans `product.supplierinfo` est `partner_id`,
  ce qui confirme le chemin v16 attendu;
- le rapprochement prix fonctionne sans `ID Externe`, comme dans
  `invoice_price_checker`, en s'appuyant sur les references fournisseur et les
  IDs numeriques Odoo.
- ecriture reelle testee sur une base v16 de test : 7 succes,
  0 avertissement, 0 erreur;
- apres rechargement de la base v16 et relance de la meme facture, le classeur
  `FC260743575_controle_odoo-8.xlsx` contient 0 ligne dans `changements_prix`;
  les 7 articles retrouves ont `prix_change=False` et
  `peut_etre_mis_a_jour=False`.
- validation v12 sans ecriture sur base de production : le classeur
  `FC260743575_controle_odoo-9.xlsx` contient 24 lignes facture, 17 articles
  trouves, 14 lignes dans `changements_prix` et 14 lignes `ready` dans
  `dry_run_prix_odoo`;
- le champ fournisseur detecte en v12 reste `name`, confirmant que le
  rapprochement prix v12 n'est pas casse.

### 2026-08-03 - Etape 4, bon de commande Odoo

Modifications locales realisees :

- preparation du bon de commande avec l'ID numerique Odoo de l'article quand il
  est disponible, tout en conservant le fallback historique par ID externe;
- adaptation de la creation API du bon de commande pour preparer les lignes a
  partir des IDs numeriques, sans dependance obligatoire a `ir.model.data`;
- ajout d'un dry-run `dry_run_purchase_order_from_review` qui verifie le
  fournisseur, les articles, les unites, les quantites et les prix avant toute
  creation de bon de commande;
- ajout dans Streamlit d'un bouton "Verifier le bon de commande sans ecriture
  dans Odoo"; la creation reelle reste bloquee tant que cette verification n'a
  pas ete faite sans erreur;
- ajout de l'onglet `dry_run_po_odoo` dans le classeur de controle lorsqu'un
  dry-run PO a ete lance;
- ajout de l'onglet `creation_po_odoo` dans le classeur de controle lorsqu'une
  creation reelle de bon de commande a ete lancee;
- correction de l'affichage Streamlit apres creation du bon de commande : le
  rapport de dry-run reste visible et le diagnostic technique de creation
  s'affiche en dessous sans masquer les informations precedentes;
- ajout dans la fenetre principale d'un etat de chargement avant analyse :
  base articles disponible ou chargee, facture fournisseur chargee, puis
  invitation a lancer l'analyse seulement quand les fichiers sont prets;
- conservation de l'onglet developpeur `bon_commande_id_numerique`, visible
  seulement avec `LMC_DEVELOPER_MODE=1`, pour comparer les valeurs de creation
  PO pendant la migration.

Validation utilisateur :

- validation v16 sans creation du bon de commande : le classeur
  `FC260743575_controle_odoo-10.xlsx` contient 7 lignes dans
  `bon_commande_a_verifier` et 7 lignes `ready` dans `dry_run_po_odoo`;
- les 7 lignes PO v16 resolvent bien les articles par ID numerique avant
  creation (`product_id_resolu_avant_creation` renseigne pour chaque ligne);
- aucune ligne du dry-run PO v16 ne depend d'un ID externe Odoo pour resoudre
  l'article;
- validation v12 sans creation du bon de commande sur base de production : le
  classeur `FC260743575_controle_odoo-11.xlsx` contient 24 lignes facture,
  17 articles trouves, 6 articles non retrouves, 1 article ambigu et 17 lignes
  dans `bon_commande_a_verifier`;
- le dry-run PO v12 contient 17 lignes `ready`, 0 avertissement et 0 erreur;
- les 17 lignes PO v12 resolvent bien les articles par ID numerique avant
  creation (`product_id_resolu_avant_creation` renseigne pour chaque ligne);
- creation reelle du bon de commande testee sur une base v16 de test; le
  classeur `FC260743575_controle_odoo-12.xlsx` conserve 7 lignes `ready` dans
  `dry_run_po_odoo` et 7 lignes dans `bon_commande_a_verifier`;
- creation reelle du bon de commande testee sur une base v12 de test de
  reference; le classeur `FC260743575_controle_odoo-13.xlsx` contient
  7 lignes dans `dry_run_po_odoo` et 7 lignes dans `creation_po_odoo`;
- les 7 lignes creees en v12 sont relues depuis Odoo avec les memes IDs
  articles et les memes quantites; les prix sont relus arrondis a 2 decimales
  par Odoo;
- en v12 comme en v16, le document cree apparait comme demande de prix non
  encore validee, ce qui confirme le comportement attendu de creation simple de
  `purchase.order`;
- la validation dry-run PO est confirmee en v12 et en v16, et la creation
  reelle PO est confirmee en v12 et en v16 sur bases de test.

### 2026-08-03 - Etape 5, nettoyage des diagnostics

Verification locale :

- les outils strictement techniques restent caches par defaut :
  `bon_commande_id_numerique` n'est exporte que si le mode developpeur est
  actif, et le diagnostic technique de creation Odoo n'est visible dans
  Streamlit qu'avec ce meme flag;
- les rapports `dry_run_prix_odoo`, `dry_run_po_odoo` et `creation_po_odoo`
  restent visibles hors mode developpeur, car ils documentent les controles
  utilisateur avant/apres ecriture Odoo et ne sont pas de simples onglets de
  debug;
- le CSV d'import manuel du bon de commande reste disponible separement de la
  creation API, afin de conserver une voie de controle manuel;
- l'onglet developpeur `bon_commande_id_numerique` est conserve et documente
  plutot que supprime, car il reste utile pour diagnostiquer une base ancienne
  ou une divergence entre ID externe historique et ID numerique Odoo.
- le repertoire local non suivi `echantillons` a ete supprime; les fichiers de
  test synchronises doivent etre places dans `data_files`;
- le message principal de chargement de base a ete clarifie : apres analyse,
  l'application affiche le nom de la base Odoo chargee et le nombre total
  d'articles, par exemple "La base articles Odoo demain-mig16 est chargee avec
  4323 articles".

Conclusion :

- les deux points de l'etape 5 sont traites : les diagnostics techniques utiles
  sont derriere le flag developpeur, et les onglets temporaires sont soit
  retires du mode utilisateur normal, soit documentes quand ils doivent rester
  accessibles aux developpeurs.

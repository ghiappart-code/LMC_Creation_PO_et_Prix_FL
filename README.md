# Creation de bons de commande et revue prix Odoo

Application Streamlit pour traiter une facture fournisseur a partir de la base articles Odoo.

Objectifs initiaux :

- lire une facture fournisseur ;
- charger une base articles Odoo depuis un fichier local ou depuis Odoo ;
- rapprocher les lignes de facture avec les articles Odoo ;
- produire un brouillon de bon de commande ;
- produire les fichiers de controle des articles non retrouves, ambigus et des changements de prix ;
- preparer les futures actions Odoo apres validation explicite.

Les identifiants Odoo reels doivent etre places dans `.streamlit/secrets.toml`.
Le fichier `.streamlit/secrets.example.toml` sert uniquement de modele.

## Lancer l'application

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Tests locaux

Les tests utilisent les fichiers places dans `echantillons/` et ne se connectent pas a Odoo.

```bash
pytest
```


from __future__ import annotations

"""Recharge la base articles Odoo locale sans lancer Streamlit.

Le script lit `.streamlit/secrets.toml`, appelle l'extraction Odoo commune, puis
écrit `echantillons/base_odoo/var_articles.data`. Il sert aux tests locaux et
évite de dupliquer la logique d'extraction dans un autre script.
"""

from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lmc_po_price.odoo_articles import (  # noqa: E402
    config_from_mapping,
    refresh_articles_database,
)


DEFAULT_SAMPLE_DATABASE_PATH = ROOT / "echantillons" / "base_odoo" / "var_articles.data"


def main() -> int:
    secrets_path = ROOT / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        print(f"Fichier introuvable : {secrets_path}", file=sys.stderr)
        print("Copiez .streamlit/secrets.example.toml vers .streamlit/secrets.toml puis remplissez les accès Odoo.", file=sys.stderr)
        return 1

    with secrets_path.open("rb") as handle:
        secrets = tomllib.load(handle)

    if "odoo" not in secrets:
        print("Section [odoo] manquante dans .streamlit/secrets.toml", file=sys.stderr)
        return 1

    output_path = DEFAULT_SAMPLE_DATABASE_PATH
    df = refresh_articles_database(config_from_mapping(secrets["odoo"]), output_path)

    print(f"Base Odoo extraite : {len(df)} lignes")
    print(f"Fichier écrit : {output_path}")
    print("Colonnes :")
    for column in df.columns:
        print(f"- {column}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
